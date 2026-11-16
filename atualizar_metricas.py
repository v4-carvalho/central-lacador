#!/usr/bin/env python3
"""Atualiza o window.__RAW do index.html a partir do GA4 (Supermetrics) e do ActiveCampaign.

Uso:
  python atualizar_metricas.py --dry-run   # busca, compara com o que esta no index.html e NAO grava
  python atualizar_metricas.py             # busca e grava no index.html

Credenciais: arquivo .env (AC_API_URL, AC_API_KEY, SUPERMETRICS_API_KEY, SUPERMETRICS_GA4_ACCOUNT_ID).

O que atualiza:
  ga4        -> Supermetrics GA4 (pedidos e receita por canal, por mes)
  campanhas  -> ActiveCampaign, somente campanhas avulsas com data no nome ("DD-MM-AAAA | 9h segmento")
  ac         -> derivado de campanhas (data, horario, segmento)
  rev_seg    -> Supermetrics GA4: receita total por "conteudo do anuncio manual da sessao"
                (onde ficam os segmentos), de REV_SEG_START ate hoje
O que NAO mexe (vem de planilha ou nao e comparavel): kpi, ltv, tests
e campanhas automaticas/sem data no nome (AC devolve totais acumulados, o painel usa recorte).
"""
import csv, io, json, os, re, sys, urllib.parse, urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(ROOT, "index.html")
DRY = "--dry-run" in sys.argv

SM_USER = "juan.augusto@v4company.com"
GA4_START = "2023-12-01"
REV_SEG_START = "2026-05-01"  # os segmentos so existem no GA4 a partir de maio/2026
REV_SEG_LABELS = {  # conteudo do anuncio (utm_content) -> rotulo exibido
    "seguro": "Seguro",
    "alto-risco": "Alto risco",
    "risco-moderado": "Risco moderado",
    "altamente-engajado": "Altamente engajado",
    "moderadamente-engajado": "Moderadamente engajado",
    "levemente-engajado": "Levemente engajado",
    "desengajado": "Desengajado",
    "sem-engajamento": "Sem engajamento",
}

NAME_RE = re.compile(r"^\s*(\d\d)-(\d\d)-(\d{4})\s*\|\s*(\d+)h\s+(.+?)\s*$")
SEGMENTS = {  # slug no nome da campanha -> rotulo usado no painel
    "seguro": "Seguro",
    "levemente-engajado": "Levemente engajado",
    "moderadamente-engajado": "Moderadamente engajado",
    "altamente-engajado": "Altamente engajado",
    "desengajado": "Desengajado",
}


def load_env():
    p = os.path.join(ROOT, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    need = ["AC_API_URL", "AC_API_KEY", "SUPERMETRICS_API_KEY", "SUPERMETRICS_GA4_ACCOUNT_ID"]
    miss = [k for k in need if not os.environ.get(k)]
    if miss:
        sys.exit("Faltando no .env: " + ", ".join(miss))


def http(url, headers, body=None):
    req = urllib.request.Request(url, headers=headers, data=body, method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- GA4
def fetch_ga4():
    body = json.dumps({
        "ds_id": "GAWA",
        "ds_accounts": [os.environ["SUPERMETRICS_GA4_ACCOUNT_ID"]],
        "ds_user": SM_USER,
        "start_date": GA4_START,
        "end_date": date.today().isoformat(),
        "fields": ["year", "month", "sessionDefaultChannelGroup", "transactions", "purchaseRevenue"],
        "max_rows": 10000,
    }).encode()
    d = http("https://api.supermetrics.com/enterprise/v2/query/data/json",
             {"Authorization": "Bearer " + os.environ["SUPERMETRICS_API_KEY"],
              "Content-Type": "application/json"}, body)
    if "error" in d:
        sys.exit("Supermetrics: " + json.dumps(d["error"]))
    rows = d["data"][1:]
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["ym", "canal", "pedidos", "receita"])
    for y, m, canal, ped, rec in rows:
        w.writerow([f"{y}|{m}", canal, ped, rec])
    return out.getvalue()


def fetch_rev_seg():
    """Linha = termo manual, coluna = conteudo do anuncio manual, valor = receita total.
    Para o grafico basta o total por coluna (conteudo) dos segmentos conhecidos."""
    body = json.dumps({
        "ds_id": "GAWA",
        "ds_accounts": [os.environ["SUPERMETRICS_GA4_ACCOUNT_ID"]],
        "ds_user": SM_USER,
        "start_date": REV_SEG_START,
        "end_date": date.today().isoformat(),
        "fields": ["sessionManualTerm", "sessionManualAdContent", "totalRevenue"],
        "max_rows": 100000,
    }).encode()
    d = http("https://api.supermetrics.com/enterprise/v2/query/data/json",
             {"Authorization": "Bearer " + os.environ["SUPERMETRICS_API_KEY"],
              "Content-Type": "application/json"}, body)
    if "error" in d:
        sys.exit("Supermetrics (rev_seg): " + json.dumps(d["error"]))
    tot = defaultdict(float)
    for _term, content, rev in d["data"][1:]:
        label = REV_SEG_LABELS.get(str(content).strip().lower().replace(" ", "-"))
        if label:
            tot[label] += rev
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["segment", "revenue"])
    for k, v in sorted(tot.items(), key=lambda kv: -kv[1]):
        w.writerow([k, round(v, 2)])
    return out.getvalue()


# ---------------------------------------------------------------- ActiveCampaign
def fetch_campaigns(since):
    base = os.environ["AC_API_URL"].rstrip("/") + "/api/3/campaigns"
    hdr = {"Api-Token": os.environ["AC_API_KEY"]}
    out, offset = [], 0
    while True:
        q = urllib.parse.urlencode({"limit": 100, "offset": offset, "orders[sdate]": "DESC"})
        page = http(f"{base}?{q}", hdr).get("campaigns", [])
        if not page:
            break
        for c in page:
            sd = c.get("sdate")
            if not sd:
                continue
            if sd[:10] < since:
                return out
            out.append(c)
        offset += 100
    return out


def to_row(c):
    sends = int(c["send_amt"])
    opens = int(c["uniqueopens"])
    clicks = int(c["subscriberclicks"])
    bounces = int(c["hardbounces"]) + int(c["softbounces"])
    unsubs = int(c["unsubscribes"])
    pct = lambda n, d: round(n / d * 100, 2) if d else 0
    sent = datetime.fromisoformat(c["ldate"] or c["sdate"]).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "bounce_rate": pct(bounces, sends), "campaign_id": int(c["id"]),
        "campaign_name": c["name"], "click_rate": pct(clicks, sends),
        "click_to_open_rate": pct(clicks, opens), "last_sent_date": sent,
        "open_rate": pct(opens, sends), "subject": c.get("subject", ""),
        "total_bounces": bounces, "total_sends": sends,
        "total_unsubscribes": unsubs, "unique_subscriber_clicks": clicks,
        "unique_total_opens": opens, "unsubscribe_rate": pct(unsubs, sends),
    }


def build_ac(campanhas):
    agg = defaultdict(lambda: [0, 0, 0, 0])
    for c in campanhas:
        m = NAME_RE.match(c["campaign_name"])
        if not m:
            continue
        dd, mm, yy, hh, seg = m.groups()
        label = SEGMENTS.get(seg.strip().lower().replace(" ", "-"))
        if not label:
            continue
        a = agg[(f"{yy}-{mm}-{dd}", f"{hh}h", label)]
        a[0] += c["total_sends"]; a[1] += c["unique_total_opens"]
        a[2] += c["unique_subscriber_clicks"]; a[3] += c["total_unsubscribes"]
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["date", "slot", "segment", "sends", "opens", "clicks", "unsubs"])
    for (d, s, g), v in sorted(agg.items(), key=lambda kv: (kv[0][0], int(kv[0][1][:-1]), kv[0][2])):
        w.writerow([d, s, g, *v])
    return out.getvalue()


# ---------------------------------------------------------------- main
RAW_RE = re.compile(r"(window\.__RAW=)(\{.*?\})(;?</script>)", re.S)


def main():
    load_env()
    html = open(HTML, encoding="utf-8").read()
    m = RAW_RE.search(html)
    raw = json.loads(m.group(2))
    old = raw["campanhas"]
    since = min(c["last_sent_date"][:10] for c in old)

    new_ga4 = fetch_ga4()
    ac_rows = [to_row(c) for c in fetch_campaigns(since)]
    dated_new = [r for r in ac_rows if NAME_RE.match(r["campaign_name"]) and r["total_sends"] > 0]
    keep_other = [r for r in old if not NAME_RE.match(r["campaign_name"])]

    # validacao: campanhas avulsas que ja estavam no painel devem bater
    old_by_id = {r["campaign_id"]: r for r in old}
    def bad(o, n):  # envios e data tem que bater; abertura/clique/descadastro so podem crescer
        r = [k for k in ("total_sends", "last_sent_date") if o[k] != n[k]]
        r += [k for k in ("unique_total_opens", "unique_subscriber_clicks", "total_unsubscribes") if n[k] < o[k]]
        return r
    diff = [(r["campaign_id"], k, old_by_id[r["campaign_id"]][k], r[k])
            for r in dated_new if r["campaign_id"] in old_by_id
            for k in bad(old_by_id[r["campaign_id"]], r)]
    both = sum(1 for r in dated_new if r["campaign_id"] in old_by_id)
    print(f"GA4: {new_ga4.count(chr(10)) - 1} linhas (antes {raw['ga4'].count(chr(10)) - 1})")
    print(f"Campanhas avulsas: {len(dated_new)} (antes {len(old) - len(keep_other)}); "
          f"mantidas como estavam: {len(keep_other)}; ja existentes comparadas: {both}; divergencias: {len(diff)}")
    for d in diff[:15]:
        print("  divergencia", d)
    print("Campanhas que sumiram do AC (mantidas do painel):",
          len([r for r in old if r["campaign_id"] not in {x["campaign_id"] for x in dated_new} and NAME_RE.match(r["campaign_name"])]))

    # campanhas que o painel ja tinha e o AC nao devolve mais (apagadas la) ficam como estavam
    ids_new = {r["campaign_id"] for r in dated_new}
    kept_old = [r for r in old if NAME_RE.match(r["campaign_name"]) and r["campaign_id"] not in ids_new]
    camp = sorted(dated_new + kept_old + keep_other, key=lambda r: r["last_sent_date"], reverse=True)
    new_raw = dict(raw, rev_seg=fetch_rev_seg(), ga4=new_ga4, campanhas=camp, ac=build_ac(camp))
    print(f"ac: {new_raw['ac'].count(chr(10)) - 1} linhas (antes {raw['ac'].count(chr(10)) - 1}); "
          f"ultima data {new_raw['ac'].strip().splitlines()[-1].split(',')[0]}")

    print("rev_seg:\n" + new_raw["rev_seg"])
    if DRY:
        print("dry-run: nada gravado")
        return
    blob = json.dumps(new_raw, ensure_ascii=False, separators=(",", ":"))
    html = html[:m.start()] + m.group(1) + blob.replace("</", "<\\/") + m.group(3) + html[m.end():]
    open(HTML, "w", encoding="utf-8", newline="").write(html)
    print("index.html atualizado")


if __name__ == "__main__":
    main()
