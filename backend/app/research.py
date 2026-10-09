"""Public-source research on the borrower athlete (Wikipedia + Spotrac).

Feeds Section V (Project Sponsorship): the memo's narrative describes the
ATHLETE — background, career path, contract history, career earnings — rather
than just the facility. This module only GATHERS source text; the narrative
itself is composed by a second Claude call in extraction.py.

How each source is fetched (verified working 2026-07-06):
- Wikipedia via httpx (always present: it is a dependency of the anthropic
  SDK). The Wikimedia API requires a DESCRIPTIVE User-Agent — a browser-
  imitating UA gets a 403.
- Spotrac sits behind CloudFront bot protection that 403s plain HTTP clients
  and Playwright's default HeadlessChrome UA. It is fetched with the same
  Playwright headless Chromium already installed for PDF export, with a
  real-browser UA on the context. The Claude API's web_search server tool is
  NOT an option here: subscription usage (OAuth) tokens get
  'web_search_tool_result_error: unavailable'.

Research is best-effort and must never break /api/extract: every failure is
swallowed (logged at WARNING) and callers fall back to whatever narrative the
deal documents provided.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Wikimedia asks API clients to identify themselves; fake browser UAs are blocked.
_WIKI_UA = "CreditMemoBuilder/1.0 (South River Capital; lauren@southrivercapital.com)"

# Spotrac's CloudFront rules block anything advertising itself as headless.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_SPOTRAC_SEARCH = "https://www.spotrac.com/search?q={query}"

# League slugs Spotrac uses as the first URL segment of player pages
# (e.g. spotrac.com/nba/player/...). Used to verify a search hit is the right
# person when the extraction knows the borrower's league.
_LEAGUE_SLUGS = ("nba", "nfl", "mlb", "nhl", "wnba", "mls", "epl", "nwsl")
_SPORT_TO_SLUG = {
    "basketball": "nba",
    "football": "nfl",
    "baseball": "mlb",
    "hockey": "nhl",
    "soccer": "mls",
}

_WIKI_MAX_CHARS = 2500
# Generous because the text now feeds TWO consumers: the Section V narrative
# and the guaranteed-salary cross-check. The year-by-year contract tables
# (base salary + what portion is guaranteed) sit below the page's summary
# block and were cut off at the old 6000-char cap.
_SPOTRAC_MAX_CHARS = 14000


def _league_slug(league: str | None, sport: str | None) -> str | None:
    """Best-effort Spotrac league slug from the extracted league/sport strings."""
    for value in (league, sport):
        if not value:
            continue
        lowered = value.lower()
        for slug in _LEAGUE_SLUGS:
            if slug in lowered.split() or lowered == slug:
                return slug
        for keyword, slug in _SPORT_TO_SLUG.items():
            if keyword in lowered:
                # Women's basketball is the one keyword collision that matters.
                return "wnba" if slug == "nba" and "women" in lowered else slug
    return None


# Generational suffixes: part of the name but never how a search index keys it,
# and never the token to treat as the surname ("jr." would match ANY junior).
_SUFFIX_TOKENS = {"jr", "sr", "ii", "iii", "iv", "v"}


def _is_initial(token: str) -> bool:
    return len(token.rstrip(".")) == 1


def _is_suffix(token: str) -> bool:
    return token.rstrip(".").lower() in _SUFFIX_TOKENS


def _core_parts(name: str) -> list[str]:
    """The tokens that identify the athlete, AS WRITTEN — middle initials and
    generational suffixes dropped, so the last element is the real surname."""
    tokens = name.split()
    core = [t for t in tokens if not _is_initial(t) and not _is_suffix(t)]
    return core or tokens


def _core_tokens(name: str) -> list[str]:
    """_core_parts, lowercased for matching."""
    return [t.lower() for t in _core_parts(name)]


def _surname_tokens(name: str) -> list[str]:
    """Every core token after the given name, lowercased and unpunctuated — the
    candidate surnames a search result may be matched on.

    A compound surname has two ("rivas", "delgado" for "Mateo Rivas Delgado"), and
    the sports world indexes the athlete under the FIRST one, so matching the
    last token alone would reject the very page we are looking for.
    """
    core = [t.rstrip(".") for t in _core_tokens(name)]
    return core[1:] or core


def _search_names(name: str) -> list[str]:
    """Search queries to try, most faithful first.

    Contracts state the LEGAL name ("Joey E. Porter Jr.") while Spotrac and
    Wikipedia index the common one ("Joey Porter Jr."), so a middle initial
    turned both searches into zero results on a real deal (2026-09-02) and
    the salary check silently fell back to the documents. Variants: as
    extracted, without middle initials, then also without the suffix.

    A compound surname gets one more. The ID — and so, since 2026-10-01, the
    extracted name — carries both surnames ("Mateo Rivas Delgado") while Spotrac
    and Wikipedia index the given name plus the FIRST one, so the trailing
    surname comes off last. Dropping the first surname instead ("Mateo Delgado")
    is deliberately NOT tried: strangers really are named that, and the
    league check would wave one of them through as the borrower.
    """
    name = " ".join(name.split())
    tokens = name.split()
    no_initials = [t for t in tokens if not _is_initial(t)]
    no_suffix = [t for t in no_initials if not _is_suffix(t)]
    core = _core_parts(name)
    variants = [name, " ".join(no_initials), " ".join(no_suffix)]
    if len(core) >= 3:
        variants.append(" ".join(core[:-1]))
    out: list[str] = []
    for variant in variants:
        if variant and variant not in out:
            out.append(variant)
    return out


def _pick_wiki_title(hits: list[dict], name: str) -> tuple[str | None, str | None]:
    """(strong_title, weak_title) for the athlete among one search's hits.

    Strong: a title carrying every non-initial token — suffix included, so
    "Joey Porter Jr." beats the father's "Joey Porter" — else every core
    token. Weak: the surname alone, which a junk hit like "Porter (surname)"
    also matches, so callers may use it only as a LAST resort after every
    search variant failed the strong test. Initials and suffixes are never
    the surname test — "jr." would match any junior. A hit sharing none of
    these means the search matched something unrelated, and feeding it to
    the memo is worse than saying nothing.
    """
    named = [t for t in name.lower().split() if not _is_initial(t)]
    core = _core_tokens(name)
    last_name = core[-1].rstrip(".")
    strong = (
        next((h["title"] for h in hits
              if all(t in h["title"].lower() for t in named)), None)
        or next((h["title"] for h in hits
                 if all(t in h["title"].lower() for t in core)), None)
    )
    weak = next((h["title"] for h in hits
                 if last_name in h["title"].lower()), None)
    return strong, weak


def wiki_lookup(name: str, sport: str | None) -> tuple[str | None, str | None]:
    """Return (intro_text, article_url) for the athlete's Wikipedia page."""
    import httpx  # dependency of the anthropic SDK, so always installed

    headers = {"User-Agent": _WIKI_UA}
    # The legal-name query can return only junk ("Porter (surname)", "Joey
    # (name)") while the common-name query finds the athlete (2026-09-02) —
    # so a variant's hits only count when one actually names the athlete,
    # and the surname-only match is a last resort after ALL variants.
    title = None
    weak_title = None
    for query in _search_names(name):
        search = httpx.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query", "list": "search", "format": "json",
                "srsearch": f"{query} {sport or ''}".strip(), "srlimit": 3,
            },
            headers=headers, timeout=20,
        )
        search.raise_for_status()
        hits = search.json().get("query", {}).get("search", [])
        # Judged against the QUERY, not the extracted name: the compound-
        # surname variant ("Mateo Rivas") is searched precisely because the
        # article never carries the second surname (Lauren, 2026-10-01).
        strong, weak = _pick_wiki_title(hits, query)
        if strong:
            title = strong
            break
        if weak_title is None:
            weak_title = weak
    title = title or weak_title
    if not title:
        return None, None

    extract_resp = httpx.get(
        "https://en.wikipedia.org/w/api.php",
        params={
            "action": "query", "prop": "extracts", "format": "json",
            "exintro": 1, "explaintext": 1, "redirects": 1, "titles": title,
        },
        headers=headers, timeout=20,
    )
    extract_resp.raise_for_status()
    pages = extract_resp.json().get("query", {}).get("pages", {})
    text = next(iter(pages.values()), {}).get("extract", "").strip()
    if not text:
        return None, None
    url = "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")
    return text[:_WIKI_MAX_CHARS], url


def _read_player_page(page, name: str,
                      slug: str | None) -> tuple[str, str] | None:
    """(page_text, url) of the player page currently loaded, or None when the
    URL's league segment says this is a different athlete than the borrower.

    Player URLs are league-scoped (spotrac.com/<league>/player/...); when we
    know the borrower's league, a mismatch means a same-surname stranger.
    """
    if slug and f"/{slug}/" not in page.url:
        return None
    text = page.inner_text("body")
    # Skip the site chrome (nav / trending lists) at the top of the body; the
    # player content starts at their name or the "Contract Details" tab strip.
    # The page prints the COMMON name, so look for the core name (no middle
    # initial / suffix) and, for a compound surname, the given name plus the
    # first surname — never the extracted legal name as written.
    lowered = text.lower()
    core = _core_tokens(name)
    forms = [" ".join(core)]
    if len(core) >= 3:
        forms.append(" ".join(core[:-1]))
    start = max([lowered.find(f) for f in forms]
                + [lowered.find("contract details")])
    return text[max(start, 0):][:_SPOTRAC_MAX_CHARS], page.url


def spotrac_lookup(name: str, league: str | None,
                   sport: str | None) -> tuple[str | None, str | None]:
    """Return (page_text, page_url) for the athlete's Spotrac player page."""
    from playwright.sync_api import sync_playwright  # imported lazily; heavy dep

    slug = _league_slug(league, sport)
    named = [t for t in name.lower().split() if not _is_initial(t)]
    core = _core_tokens(name)
    given = core[0]
    surnames = _surname_tokens(name)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(user_agent=_BROWSER_UA, locale="en-US")
            page = ctx.new_page()
            # Spotrac indexes the COMMON name; searching the extracted legal
            # one ("Joey E. Porter Jr.") can return nothing at all
            # (2026-09-02, a real deal). Try the name as extracted, then
            # without the middle initial, then without the suffix.
            for query in _search_names(name):
                page.goto(_SPOTRAC_SEARCH.format(query=query.replace(" ", "%20")),
                          wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                # A single-match search redirects STRAIGHT to the player page —
                # no result links to click (seen 2026-08-20 on a real deal: the
                # old link-hunt found nothing and the whole lookup silently
                # returned None). Read the page we landed on.
                if "/player/" in page.url:
                    got = _read_player_page(page, name, slug)
                    if got:
                        return got
                    continue
                candidates = page.eval_on_selector_all(
                    "a[href*='/player/']",
                    "els => els.map(e => ({href: e.href, text: (e.innerText || '').trim()}))",
                )
                # A result counts as the athlete only if it carries one of
                # their surnames — never a suffix like "jr.", which would match
                # any junior on the page, and for a compound surname either
                # half, since the page prints only the first.
                matches = [c for c in candidates
                           if any(s in c["text"].lower() for s in surnames)]
                # Fullest match first: a sibling or teammate sharing the
                # surname (e.g. Luke Hughes vs Jack Hughes, both NJ Devils) can
                # otherwise outrank the borrower in Spotrac's result order and
                # would pass the league check below. Suffix-carrying matches
                # outrank core-name ones (Joey Porter Jr. vs his father), and a
                # result naming the athlete's GIVEN name outranks a stranger
                # who merely shares a surname ("Mateo Rivas" over "A. Delgado").
                ordered: list[dict] = []
                for tier in ([c for c in matches
                              if all(t in c["text"].lower() for t in named)],
                             [c for c in matches
                              if all(t in c["text"].lower() for t in core)],
                             [c for c in matches if given in c["text"].lower()],
                             matches):
                    for cand in tier:
                        if cand not in ordered:
                            ordered.append(cand)

                for cand in ordered[:4]:
                    page.goto(cand["href"], wait_until="domcontentloaded",
                              timeout=45000)
                    page.wait_for_timeout(2500)
                    got = _read_player_page(page, name, slug)
                    if got:
                        return got
        finally:
            browser.close()
    return None, None


def _backend_python() -> str:
    """The venv's own python.exe — never sys.executable.

    The live server is a re-exec'd child whose sys.executable points at the
    BASE Python312 install (see run_local.py), which has no playwright; the
    venv interpreter next to the app does.
    """
    venv = Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "python.exe"
    return str(venv) if venv.exists() else sys.executable


def _spotrac_lookup_subprocess(name: str, league: str | None,
                               sport: str | None) -> tuple[str | None, str | None]:
    """spotrac_lookup, run in a FRESH python process.

    Kept as belt-and-suspenders against Playwright launch failures. The
    original trigger — a false "Executable doesn't exist" — was root-caused
    2026-08-25 as MSIX filesystem virtualization hiding the browser install
    from non-packaged processes; see memo._render_pdf_subprocess for the
    story. Retried once via ``python -m app.research`` (__main__ below).
    """
    proc = subprocess.run(
        [_backend_python(), "-m", "app.research", name, league or "", sport or ""],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip()[-400:]
                           or f"subprocess exit {proc.returncode}")
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    return data.get("text"), data.get("url")


def gather_athlete_research(name: str | None, sport: str | None = None,
                            league: str | None = None) -> dict:
    """Collect public source text about the athlete. Never raises."""
    out: dict = {"wiki_text": None, "wiki_url": None,
                 "spotrac_text": None, "spotrac_url": None}
    if not name or not name.strip():
        return out
    name = name.strip()

    try:
        out["wiki_text"], out["wiki_url"] = wiki_lookup(name, sport)
    except Exception as exc:  # noqa: BLE001 - research must never break extraction
        logger.warning("Wikipedia lookup failed for %s: %s", name, exc)
    if not out["wiki_text"]:
        logger.info("Wikipedia lookup found no article for %s", name)

    try:
        out["spotrac_text"], out["spotrac_url"] = spotrac_lookup(name, league, sport)
    except Exception as exc:  # noqa: BLE001 - research must never break extraction
        logger.warning("Spotrac lookup failed for %s: %s — retrying in a "
                       "fresh process", name, exc)
        try:
            out["spotrac_text"], out["spotrac_url"] = _spotrac_lookup_subprocess(
                name, league, sport)
        except Exception as exc2:  # noqa: BLE001
            logger.warning("Spotrac fresh-process lookup also failed for %s: %s",
                           name, exc2)
    # A clean no-result is as load-bearing as a crash — it silently demotes
    # the salary check to the documents (the backup), so say it in the log.
    if not out["spotrac_text"]:
        logger.warning("Spotrac lookup found no player page for %s — the "
                       "guaranteed-salary check falls back to the documents",
                       name)

    return out


if __name__ == "__main__":
    # Subprocess entry for _spotrac_lookup_subprocess:
    #   python -m app.research <name> [league] [sport]
    # Prints one JSON line {"text": ..., "url": ...} to stdout (forced UTF-8 —
    # player pages carry accented names the Windows console codepage rejects).
    sys.stdout.reconfigure(encoding="utf-8")
    _name = sys.argv[1]
    _league = sys.argv[2] if len(sys.argv) > 2 else ""
    _sport = sys.argv[3] if len(sys.argv) > 3 else ""
    _text, _url = spotrac_lookup(_name, _league or None, _sport or None)
    print(json.dumps({"text": _text, "url": _url}))
