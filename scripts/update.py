#!/usr/bin/env python3
"""Gera os SVGs e as seções automáticas do README do perfil.

Uso:
  python scripts/update.py                  # coleta pela API do GitHub e renderiza
  python scripts/update.py --render-only    # renderiza a partir de assets/data.json
  python scripts/update.py --local DIR --email E   # coleta de clones locais (desenvolvimento)

Textos, projetos em destaque e stack ficam em profile.json.
"""
import argparse
import base64
import datetime as dt
import html
import io
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
FONTS = ASSETS / "fonts"
TZ = ZoneInfo("America/Sao_Paulo")
MESES = "jan fev mar abr mai jun jul ago set out nov dez".split()
MESES_LONGOS = ("janeiro fevereiro março abril maio junho julho agosto "
                "setembro outubro novembro dezembro").split()

# --------------------------------------------------------------------------
# Coleta
# --------------------------------------------------------------------------

TOKEN = os.environ.get("PROFILE_TOKEN") or os.environ.get("GITHUB_TOKEN")
# Com PROFILE_TOKEN (token pessoal só de leitura) os repositórios privados também entram.
SEES_PRIVATE = bool(os.environ.get("PROFILE_TOKEN"))


def api(path):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "profile-readme"})
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def paged(path):
    out, page = [], 1
    sep = "&" if "?" in path else "?"
    while True:
        data = api(f"{path}{sep}per_page=100&page={page}")
        out += data
        if len(data) < 100:
            return out
        page += 1


def branch_commits(full, author=None):
    """Commits de todos os branches, sem repetir SHA: {sha: data}."""
    found = {}
    for b in paged(f"/repos/{full}/branches"):
        query = f"/repos/{full}/commits?sha={urllib.parse.quote(b['name'], safe='')}"
        if author:
            query += f"&author={author}"
        for c in paged(query):
            found[c["sha"]] = c["commit"]["author"]["date"][:10]
    return found


def collect_api(cfg, previous):
    user = cfg["user"]
    skip = {f"{user}/{user}".lower()} | {r.lower() for r in cfg.get("exclude_repos", [])}
    listing = "/user/repos?affiliation=owner" if SEES_PRIVATE else f"/users/{user}/repos?type=owner"
    metas = [r for r in paged(listing) if not r["fork"]]
    repos, seen = [], set()
    for full in cfg.get("extra_repos", []):
        try:
            metas.append(api(f"/repos/{full}"))
        except urllib.error.HTTPError as e:
            if e.code not in (403, 404):  # token sem acesso a um privado de outra conta
                raise
            print(f"aviso: sem acesso a {full}, mantendo os dados anteriores")
            repos += [r for r in previous if r["full"].lower() == full.lower()]
            seen.add(full.lower())
    for m in metas:
        full = m["full_name"]
        if full.lower() in skip or full.lower() in seen:
            continue
        seen.add(full.lower())
        try:
            mine = branch_commits(full, user)
            total = len(branch_commits(full))
        except urllib.error.HTTPError as e:
            if e.code != 409:  # 409 = repositório vazio
                raise
            mine, total = {}, 0
        repos.append({
            "full": full, "url": m["html_url"], "private": m["private"],
            "description": m.get("description") or "",
            "language": m.get("language") or "",
            "commits": sorted(mine.values()),
            "total": total,
        })
    if not SEES_PRIVATE:
        # sem acesso aos privados: mantém o último retrato conhecido deles
        repos += [r for r in previous if r.get("private") and r["full"].lower() not in seen
                  and r["full"].lower() not in skip]
    return repos


EXT_LANG = {"java": "Java", "ts": "TypeScript", "tsx": "TypeScript", "js": "JavaScript",
            "jsx": "JavaScript", "cpp": "C++", "h": "C++", "css": "CSS", "html": "HTML"}


def collect_local(directory, email, private):
    repos = []
    for d in sorted(Path(directory).iterdir()):
        if not (d / ".git").exists():
            continue
        git = lambda *a: subprocess.run(["git", "-C", str(d), *a], capture_output=True,
                                        text=True, check=True).stdout
        url = git("remote", "get-url", "origin").strip().removesuffix(".git")
        full = "/".join(url.split("/")[-2:])
        exts = Counter(Path(f).suffix.lstrip(".").lower() for f in git("ls-files").split())
        langs = Counter()
        for ext, n in exts.items():
            if ext in EXT_LANG:
                langs[EXT_LANG[ext]] += n
        repos.append({
            "full": full, "url": url, "private": full in private, "description": "",
            "language": langs.most_common(1)[0][0] if langs else "",
            "commits": sorted(c[:10] for c in
                              git("log", "--all", f"--author={email}", "--format=%aI").split()),
            "total": int(git("rev-list", "--all", "--count")),
        })
    return repos

# --------------------------------------------------------------------------
# Fontes e SVG
# --------------------------------------------------------------------------

FACES = {  # chave: (família CSS, peso, arquivo)
    "d400": ("Bricolage", 400, "bricolage-grotesque-latin-400-normal.woff2"),
    "d500": ("Bricolage", 500, "bricolage-grotesque-latin-500-normal.woff2"),
    "d700": ("Bricolage", 700, "bricolage-grotesque-latin-700-normal.woff2"),
    "d800": ("Bricolage", 800, "bricolage-grotesque-latin-800-normal.woff2"),
    "m400": ("JBMono", 400, "jetbrains-mono-latin-400-normal.woff2"),
    "m500": ("JBMono", 500, "jetbrains-mono-latin-500-normal.woff2"),
}
FALLBACK = {
    "Bricolage": "'Bricolage', -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif",
    "JBMono": "'JBMono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
}

THEMES = {
    "light": {"bg": "#ffffff", "panel": "#f6f8fa", "line": "#d1d9e0", "grid": "#eaeef2",
              "ink": "#1f2328", "ink2": "#4b535d", "muted": "#656d76", "accent": "#cf4a1f"},
    "dark": {"bg": "#0d1117", "panel": "#151b23", "line": "#3d444d", "grid": "#21262d",
             "ink": "#f0f6fc", "ink2": "#c3cbd4", "muted": "#9198a1", "accent": "#ff7b4f"},
}

_metrics = {}


def text_width(s, face, size, ls=0.0):
    if face not in _metrics:
        f = TTFont(FONTS / FACES[face][2])
        _metrics[face] = (f.getBestCmap(), f["hmtx"].metrics, f["head"].unitsPerEm)
    cmap, hmtx, upm = _metrics[face]
    units = sum(hmtx[cmap.get(ord(ch), ".notdef")][0] for ch in s)
    return units * size / upm + ls * len(s)


def wrap(s, face, size, width, max_lines):
    lines, cur = [], ""
    for word in s.split():
        trial = f"{cur} {word}".strip()
        if text_width(trial, face, size) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while text_width(last + "…", face, size) > width:
            last = last[:-1]
        lines[-1] = last.rstrip(" ,.;:") + "…"
    return lines


def esc(s):
    return html.escape(str(s), quote=True)


class Svg:
    def __init__(self, w, h, theme, title, margin=(0, 0, 0, 0)):
        """margin = (esquerda, topo, direita, base), área transparente em volta do cartão."""
        self.w, self.h, self.c, self.title = w, h, THEMES[theme], title
        self.margin = margin
        self.parts, self.used = [], defaultdict(set)

    def col(self, key):
        return self.c.get(key, key)

    def add(self, raw):
        self.parts.append(raw)

    def text(self, x, y, s, face="d400", size=14, fill="ink", anchor="start", ls=0.0):
        fam, weight, _ = FACES[face]
        self.used[face].update(s)
        extra = f' letter-spacing="{ls}"' if ls else ""
        if anchor != "start":
            extra += f' text-anchor="{anchor}"'
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FALLBACK[fam]}" '
                 f'font-weight="{weight}" font-size="{size}" fill="{self.col(fill)}"{extra}>'
                 f'{esc(s)}</text>')

    def card(self):
        self.add(f'<rect x="0.5" y="0.5" width="{self.w - 1}" height="{self.h - 1}" rx="12" '
                 f'fill="{self.c["bg"]}" stroke="{self.c["line"]}"/>')

    def render(self):
        faces = []
        for key, chars in sorted(self.used.items()):
            fam, weight, file = FACES[key]
            font = TTFont(FONTS / file)
            opts = subset.Options()
            opts.flavor = "woff2"
            opts.name_IDs = []
            sub = subset.Subsetter(opts)
            sub.populate(text="".join(sorted(chars)))
            sub.subset(font)
            buf = io.BytesIO()
            font.save(buf)
            b64 = base64.b64encode(buf.getvalue()).decode()
            faces.append(f"@font-face{{font-family:'{fam}';font-weight:{weight};"
                         f"src:url(data:font/woff2;base64,{b64}) format('woff2');}}")
        ml, mt, mr, mb = self.margin
        W, H = self.w + ml + mr, self.h + mt + mb
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
                f'viewBox="0 0 {W} {H}" role="img" aria-label="{esc(self.title)}">'
                f'<title>{esc(self.title)}</title><style>{"".join(faces)}</style>'
                f'<g transform="translate({ml} {mt})">' + "".join(self.parts) + "</g></svg>\n")

# --------------------------------------------------------------------------
# Dados derivados
# --------------------------------------------------------------------------

def d(s):
    return dt.date.fromisoformat(s[:10])


def mes_ano(day, longo=False):
    return f"{(MESES_LONGOS if longo else MESES)[day.month - 1]} {day.year}"


def enrich(data, cfg):
    info = cfg.get("repos", {})
    for r in data["repos"]:
        meta = info.get(r["full"], {})
        owner, name = r["full"].split("/")
        r["name"] = name
        r["owner"] = owner
        r["title"] = meta.get("title", name)
        r["about"] = meta.get("about") or r["description"] or "Sem descrição."
        r["blurb"] = meta.get("blurb", "")
        r["stack"] = meta.get("stack") or ([r["language"]] if r["language"] else [])
        r["team"] = meta.get("team", owner.lower() != cfg["user"].lower())
        r["private"] = r.get("private", False)
        r["last"] = r["commits"][-1] if r["commits"] else ""
    data["repos"].sort(key=lambda r: (r["last"], len(r["commits"])), reverse=True)
    return data


def month_domain(data):
    days = [d(c) for r in data["repos"] for c in r["commits"]]
    today = d(data["generated"])
    start = min(days, default=today).replace(day=1)
    months, m = [], start
    while m <= today:
        months.append(m)
        m = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return months, months[-1].replace(day=28) + dt.timedelta(days=4)

# --------------------------------------------------------------------------
# Peças
# --------------------------------------------------------------------------

def header(data, cfg, theme):
    repos = data["repos"]
    n_commits = sum(len(r["commits"]) for r in repos)
    first = min((d(r["commits"][0]) for r in repos if r["commits"]), default=None)
    team = sum(r["team"] for r in repos)
    W, H = 840, 262
    s = Svg(W, H, theme, header_alt(data, cfg))
    c = s.c
    s.card()

    # linha de contexto
    s.add(f'<circle cx="36" cy="37" r="4" fill="{c["accent"]}"/>')
    s.text(48, 42, cfg["user"], "m500", 13, "ink2")
    s.text(W - 32, 42, "atualizado em " + fmt_day(d(data["generated"])), "m400", 12,
           "muted", "end")

    # nome e papel
    s.text(29, 118, cfg["name"], "d800", 60, "ink", ls=-1.5)
    s.text(32, 154, cfg["role"], "d500", 21, "ink2")
    x = 32
    for i, item in enumerate(cfg["headline"]):
        if i:
            s.add(f'<circle cx="{x + 8:.1f}" cy="178" r="2" fill="{c["accent"]}"/>')
            x += 16
        s.text(x, 182.5, item, "m400", 13, "muted")
        x += text_width(item, "m400", 13)

    # motivo: grafo de branches com HEAD
    git_motif(s, 604, 70)

    # faixa de números
    s.add(f'<line x1="1" x2="{W - 1}" y1="206.5" y2="206.5" stroke="{c["line"]}"/>')
    stats = [(str(n_commits), "commits"), (str(len(repos)), "repositórios"),
             (str(team), "em equipe"),
             (mes_ano(first) if first else "-", "primeiro commit")]
    widths = [text_width(n, "d700", 22) + 8 + text_width(l, "m400", 12) for n, l in stats]
    gap = (W - 64 - sum(widths)) / (len(stats) - 1)
    x = 32
    for i, ((num, label), w) in enumerate(zip(stats, widths)):
        if i:
            s.add(f'<line x1="{x - gap / 2:.1f}" x2="{x - gap / 2:.1f}" y1="222" y2="248" '
                  f'stroke="{c["line"]}"/>')
        s.text(x, 243, num, "d700", 22, "ink")
        s.text(x + text_width(num, "d700", 22) + 8, 242, label, "m400", 12, "muted")
        x += w + gap
    return s.render()


def git_motif(s, x0, y0):
    """Pequeno grafo de commits: main + duas branches que voltam para a main."""
    c = s.c
    main_y, a_y, b_y = y0 + 90, y0 + 50, y0 + 12
    stroke = 'fill="none" stroke-width="2" stroke-linecap="round"'
    s.add(f'<path d="M{x0} {main_y}H{x0 + 200}" stroke="{c["line"]}" {stroke}/>')
    s.add(f'<path d="M{x0 + 30} {main_y}C{x0 + 52} {main_y} {x0 + 44} {a_y} {x0 + 66} {a_y}'
          f'H{x0 + 114}C{x0 + 136} {a_y} {x0 + 128} {main_y} {x0 + 150} {main_y}" '
          f'stroke="{c["line"]}" {stroke}/>')
    s.add(f'<path d="M{x0 + 66} {a_y}C{x0 + 86} {a_y} {x0 + 80} {b_y} {x0 + 100} {b_y}'
          f'H{x0 + 160}C{x0 + 182} {b_y} {x0 + 178} {main_y} {x0 + 200} {main_y}" '
          f'stroke="{c["accent"]}" {stroke} opacity="0.9"/>')
    nodes = [(x0, main_y), (x0 + 30, main_y), (x0 + 90, a_y), (x0 + 150, main_y),
             (x0 + 124, b_y)]
    for x, y in nodes:
        s.add(f'<circle cx="{x}" cy="{y}" r="5" fill="{c["bg"]}" stroke="{c["muted"]}" '
              f'stroke-width="2"/>')
    s.add(f'<circle cx="{x0 + 200}" cy="{main_y}" r="7" fill="{c["accent"]}" '
          f'stroke="{c["bg"]}" stroke-width="3"/>')
    s.text(x0 + 200, main_y + 27, "HEAD", "m500", 12, "muted", "middle")


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def lock(s, x, cy):
    """Cadeado de 9x11px (repositório privado), desenhado em vetor."""
    c = s.c["muted"]
    s.add(f'<path d="M{x + 2} {cy - 1}v-2.5a2.5 2.5 0 0 1 5 0v2.5" fill="none" stroke="{c}" '
          f'stroke-width="1.5"/><rect x="{x}" y="{cy - 1}" width="9" height="7" rx="1.5" fill="{c}"/>')


def fmt_day(day):
    return f"{day.day} {MESES[day.month - 1]} {day.year}"


def card_alt(r):
    mine = len(r["commits"])
    count = (f'{mine} de {plural(r["total"], "commit")}' if r["total"] > mine
             else plural(mine, "commit"))
    status = " Privado." if r["private"] else (" Projeto em equipe." if r["team"] else "")
    return f'{r["title"]}: {r.get("blurb") or r["about"]}{status} {count}.'


def project_card(r, data, theme, side):
    W, H = 412, 212
    mine = len(r["commits"])
    # dois cartões por linha somam 840px e ficam alinhados às bordas das outras peças;
    # a margem de baixo soma com a folga da linha do texto e iguala o espaço entre colunas
    margin = (0, 0, 8, 11) if side == "left" else (8, 0, 0, 11)
    s = Svg(W, H, theme, card_alt(r), margin=margin)
    c = s.c
    s.card()
    pad = 24

    s.text(pad, 38, f'{r["owner"]}/{r["name"]}', "m400", 12, "muted")
    if r["team"] or r["private"]:
        label = "privado" if r["private"] else "em equipe"
        tw = text_width(label, "m500", 12) + 18
        s.add(f'<rect x="{W - pad - tw:.1f}" y="23" width="{tw:.1f}" height="22" rx="11" '
              f'fill="none" stroke="{c["line"]}"/>')
        s.text(W - pad - tw / 2, 38, label, "m500", 12, "ink2", "middle")

    s.text(pad - 1, 74, r["title"], "d700", 25, "ink", ls=-0.4)
    for i, line in enumerate(wrap(r.get("blurb") or r["about"], "d400", 15, W - 2 * pad, 2)):
        s.text(pad, 102 + i * 21, line, "d400", 15, "ink2")

    # chips de stack: só os que cabem (a tabela lista todos)
    x, y = pad, 144
    for item in r["stack"]:
        w = text_width(item, "m400", 12) + 18
        if x + w > W - pad:
            break
        s.add(f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="24" rx="6" '
              f'fill="{c["panel"]}" stroke="{c["line"]}"/>')
        s.text(x + w / 2, y + 16.5, item, "m400", 12, "ink2", "middle")
        x += w + 6

    # rodapé
    s.add(f'<line x1="1" x2="{W - 1}" y1="182.5" y2="182.5" stroke="{c["grid"]}"/>')
    label = (f'{mine} de {plural(r["total"], "commit")}' if r["total"] > mine
             else plural(mine, "commit"))
    s.text(pad, 201, label, "m500", 12, "ink")
    if r["last"]:
        s.text(W - pad, 201, "último em " + fmt_day(d(r["last"])), "m400", 12, "muted", "end")
    return s.render()


def timeline(data, theme):
    repos = data["repos"]
    months, end = month_domain(data)
    W = 840
    top, row, axis = 66, 28, 52
    H = top + row * len(repos) + axis
    s = Svg(W, H, theme, timeline_alt(data))
    c = s.c
    s.card()
    label_w = max(text_width(r["title"], "d500", 14) + (18 if r["private"] else 0) for r in repos)
    x0, x1 = 28 + label_w + 28, W - 72
    span = (end - months[0]).days
    week_px = (x1 - x0) * 7 / span
    bar_w = max(3.0, min(8.0, week_px - 2.5))  # sempre sobra espaço entre semanas

    def X(day):
        return x0 + (x1 - x0) * (day - months[0]).days / span

    def bar_h(n):  # 1 commit = 5px ... 9 ou mais = 20px
        return 5 + (min(n, 9) - 1) * 15 / 8

    # legenda
    intro = "Cada barra é uma semana; a altura é o número de commits:"
    s.text(28, 38, intro, "m400", 12, "muted")
    lx = 28 + text_width(intro, "m400", 12) + 12
    for n in (1, 5, 9):
        label = "9+" if n == 9 else str(n)
        s.add(f'<rect x="{lx:.1f}" y="{42 - bar_h(n):.1f}" width="{bar_w:.1f}" '
              f'height="{bar_h(n):.1f}" rx="1.5" fill="{c["accent"]}"/>')
        lx += bar_w + 5
        s.text(lx, 42, label, "m400", 12, "muted")
        lx += text_width(label, "m400", 12) + 12
    s.text(x1 + 44, 38, "commits", "m400", 12, "muted", "end")

    # grade de meses
    y_top, y_bot = top, top + row * len(repos)
    for i, m in enumerate(months + [end]):
        x = X(m)
        s.add(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{y_top}" y2="{y_bot + 6}" '
              f'stroke="{c["grid"]}"/>')
        if i < len(months):
            mid = X(m + dt.timedelta(days=15))
            s.text(mid, y_bot + 24, MESES[m.month - 1], "m400", 12, "muted", "middle")
            if i == 0 or m.month == 1:
                s.text(mid, y_bot + 41, str(m.year), "m500", 12, "ink2", "middle")

    # raias: barras semanais apoiadas na linha de base
    for i, r in enumerate(repos):
        base = top + row * i + row - 6
        s.text(28, base - 2, r["title"], "d500", 14, "ink")
        if r["private"]:
            lock(s, 28 + text_width(r["title"], "d500", 14) + 8, base - 6)
        s.text(x1 + 44, base - 2, str(len(r["commits"])), "m500", 12, "ink", "end")
        if not r["commits"]:
            continue
        weeks = Counter()
        for cday in r["commits"]:
            day = d(cday)
            weeks[day - dt.timedelta(days=day.weekday())] += 1
        xs = [X(w) for w in weeks]
        s.add(f'<line x1="{min(xs):.1f}" x2="{max(xs) + bar_w:.1f}" y1="{base + 0.5}" '
              f'y2="{base + 0.5}" stroke="{c["line"]}"/>')
        for w, n in sorted(weeks.items()):
            h = bar_h(n)
            s.add(f'<rect x="{X(w) + 1:.1f}" y="{base - h:.1f}" width="{bar_w:.1f}" '
                  f'height="{h:.1f}" rx="1.5" fill="{c["accent"]}"/>')
    return s.render()


def stack_usage(data):
    return Counter(t for r in data["repos"] for t in r["stack"])


def stack_card(data, cfg, theme):
    usage = stack_usage(data)
    peak = max(usage.values(), default=1)
    groups = cfg["stack_groups"]
    W = 840
    rows = max(len(g["items"]) for g in groups)
    H = 96 + rows * 32 + 12
    s = Svg(W, H, theme, stack_alt(data, cfg))
    c = s.c
    s.card()
    s.text(28, 38, "Em quantos repositórios cada tecnologia aparece", "m400", 12, "muted")
    gap = 24
    colw = (W - 56 - gap * (len(groups) - 1)) / len(groups)
    for gi, g in enumerate(groups):
        x = 28 + gi * (colw + gap)
        s.text(x, 74, g["label"].upper(), "m500", 12, "muted", ls=0.6)
        items = sorted(g["items"], key=lambda t: -usage.get(t, 0))
        for i, item in enumerate(items):
            y = 106 + i * 32
            n = usage.get(item, 0)
            s.text(x, y, item, "d500", 15, "ink")
            s.text(x + colw, y, str(n), "m500", 12, "ink2", "end")
            # a linha de baixo vira um medidor: trecho laranja proporcional ao uso
            s.add(f'<line x1="{x}" x2="{x + colw:.1f}" y1="{y + 10.5}" y2="{y + 10.5}" '
                  f'stroke="{c["grid"]}" stroke-width="2"/>')
            if n:
                s.add(f'<line x1="{x}" x2="{x + colw * n / peak:.1f}" y1="{y + 10.5}" '
                      f'y2="{y + 10.5}" stroke="{c["accent"]}" stroke-width="2"/>')
    return s.render()

# --------------------------------------------------------------------------
# README
# --------------------------------------------------------------------------

def picture(base, alt, width="100%"):
    return (f'<picture><source media="(prefers-color-scheme: dark)" srcset="assets/{base}-dark.svg">'
            f'<img alt="{esc(alt)}" src="assets/{base}-light.svg" width="{width}"></picture>')


def card_base(full):
    return "card-" + re.sub(r"[^a-z0-9]+", "-", full.lower()).strip("-")


def header_alt(data, cfg):
    repos = data["repos"]
    n = sum(len(r["commits"]) for r in repos)
    return (f'{cfg["name"]}, {cfg["role"]}. {", ".join(cfg["headline"])}. '
            f'{plural(n, "commit")} em {len(repos)} repositórios.')


def timeline_alt(data):
    repos = data["repos"]
    months, _ = month_domain(data)
    n = sum(len(r["commits"]) for r in repos)
    return (f"Linha do tempo com {plural(n, 'commit')} em {len(repos)} repositórios, de "
            f"{mes_ano(months[0], True)} a {mes_ano(months[-1], True)}. "
            "Os números de cada repositório estão na tabela abaixo.")


def stack_alt(data, cfg):
    usage = stack_usage(data)
    return " ".join(
        f'{g["label"]}: ' + ", ".join(f"{t} ({plural(usage.get(t, 0), 'repositório')})"
                                     for t in g["items"]) + "."
        for g in cfg["stack_groups"])


def header_block(data, cfg):
    return picture("header", header_alt(data, cfg))


def timeline_block(data):
    return picture("timeline", timeline_alt(data))


def stack_block(data, cfg):
    return picture("stack", stack_alt(data, cfg))


def featured_block(data, cfg):
    by = {r["full"]: r for r in data["repos"]}
    items = []
    for full in cfg["featured"]:
        r = by.get(full)
        if not r:
            continue
        pic = picture(card_base(full), card_alt(r), "50%")
        items.append(pic if r["private"] else f'<a href="{r["url"]}">{pic}</a>')
    # sem espaço entre os links: 50% + 50% precisam caber na mesma linha
    return "<p>" + "".join(items) + "</p>"


def table_block(data):
    lines = ["| Projeto | Sobre | Commits |", "| :-- | :-- | --: |"]
    for r in data["repos"]:
        mine = len(r["commits"])
        commits = f"**{mine}**" + (f'&nbsp;de&nbsp;{r["total"]}' if r["total"] > mine else "")
        if r["last"]:
            commits += f'<br><sub>último:&nbsp;{mes_ano(d(r["last"])).replace(" ", "&nbsp;")}</sub>'
        stack = " ".join(f"`{t}`" for t in r["stack"])
        if r["private"]:
            where = "<br><sub>privado</sub>"
        elif r["team"]:
            where = f'<br><sub>em equipe · {r["owner"]}</sub>'
        else:
            where = ""
        name = f'**{r["title"]}**' if r["private"] else f'[**{r["title"]}**]({r["url"]})'
        lines.append(f'| {name}{where} | {r["about"]}<br>{stack} | {commits} |')
    total = sum(len(r["commits"]) for r in data["repos"])
    n_priv = sum(r["private"] for r in data["repos"])
    priv = f" ({n_priv} privados, sem link)" if n_priv > 1 else (" (1 privado, sem link)" if n_priv else "")
    lines += ["", f'<sub>{total} commits meus em {len(data["repos"])} repositórios{priv}, '
                  f'somando todos os branches. Atualizado automaticamente '
                  f'em {fmt_day(d(data["generated"]))}.</sub>']
    return "\n".join(lines)


def replace_block(text, name, body):
    pattern = re.compile(rf"(<!-- {name}:start -->).*?(<!-- {name}:end -->)", re.S)
    if not pattern.search(text):
        raise SystemExit(f"Marcador '{name}' não encontrado no README.md")
    return pattern.sub(lambda m: f"{m.group(1)}\n{body}\n{m.group(2)}", text)


def render_all(data, cfg):
    data = enrich(data, cfg)
    by = {r["full"]: r for r in data["repos"]}
    for old in ASSETS.glob("card-*.svg"):
        old.unlink()
    for theme in THEMES:
        (ASSETS / f"header-{theme}.svg").write_text(header(data, cfg, theme))
        (ASSETS / f"timeline-{theme}.svg").write_text(timeline(data, theme))
        (ASSETS / f"stack-{theme}.svg").write_text(stack_card(data, cfg, theme))
        for i, full in enumerate(f for f in cfg["featured"] if f in by):
            side = "left" if i % 2 == 0 else "right"
            (ASSETS / f"{card_base(full)}-{theme}.svg").write_text(
                project_card(by[full], data, theme, side))
    readme = ROOT / "README.md"
    text = readme.read_text()
    text = replace_block(text, "header", header_block(data, cfg))
    text = replace_block(text, "timeline", timeline_block(data))
    text = replace_block(text, "stack", stack_block(data, cfg))
    text = replace_block(text, "featured", featured_block(data, cfg))
    text = replace_block(text, "repos", table_block(data))
    readme.write_text(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("--local")
    ap.add_argument("--email")
    ap.add_argument("--private", default="", help="repositórios privados no modo --local")
    ap.add_argument("--date", help="data de geração (AAAA-MM-DD), para testes")
    args = ap.parse_args()
    cfg = json.loads((ROOT / "profile.json").read_text())
    data_file = ASSETS / "data.json"
    if args.render_only:
        data = json.loads(data_file.read_text())
    else:
        previous = json.loads(data_file.read_text())["repos"] if data_file.exists() else []
        repos = (collect_local(args.local, args.email, args.private.split(",")) if args.local
                 else collect_api(cfg, previous))
        generated = args.date or dt.datetime.now(TZ).date().isoformat()
        data = {"generated": generated, "repos": repos}
        data_file.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    render_all(data, cfg)


if __name__ == "__main__":
    main()
