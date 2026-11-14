"""Which denomination (samfund) does a church belong to?

Evidence, strongest first:
  1. The denomination Kyrktorget gives ("Pingströrelsen, Equmeniakyrkan" -> both)
  2. The e-mail and website domains (@svenskakyrkan.se, equmeniakyrkan.se, efs.nu …)
  3. The church's name ("Pingstkyrkan", "S:t Eriks katolska församling", "… pastorat" …)
  4. The church's own website mentioning its denomination
A church can belong to more than one (shared or ecumenical congregations).
"""
from __future__ import annotations

import re

# (label, matches Kyrktorget's denomination text, matches domains, matches the name, matches website text)
FAMILIES: list[tuple[str, str, str, str | None, str | None]] = [
    ("Svenska kyrkan", r"svenska kyrkan|sjömanskyrkan", r"svenskakyrkan\.se", r"^svenska kyrkan|sjömanskyrkan", None),
    ("Katolska kyrkan", r"katolsk", r"katolskakyrkan\.se|katolsk", r"katolsk|catholic", None),
    ("Ortodoxa kyrkor", r"ortodox", r"ortodox|orthodox", r"ortodox|orthodox", None),
    ("EFS", r"\bEFS\b", r"\befs(syd|nord|mitt|vast|ost)?\.(nu|se|org)", r"\bEFS\b", r"\bEFS\b|Evangeliska Fosterlands"),
    ("Equmeniakyrkan", r"equmenia", r"equmenia", r"equmenia|\bSMU\b", r"Equmenia"),
    ("Pingströrelsen", r"pingst", r"pingst", r"pingst|filadelfia", r"Pingst ?- ?fria församlingar|Pingströrelsen|Pingst FFS"),
    ("Evangeliska Frikyrkan", r"evangeliska frikyrkan", r"\befk\.se|evangeliskafrikyrkan", None, r"Evangeliska Frikyrkan|\bEFK\b"),
    ("Alliansmissionen", r"alliansmissionen", r"alliansmissionen|\bsam\.se", r"allians", r"Alliansmissionen"),
    ("Frälsningsarmén", r"frälsningsarm", r"fralsningsarmen", r"frälsningsarm", None),
    ("ELM", r"\bELM\b|bibeltrogna", r"\belm\.|bibeltrogna", r"\bELM\b|bibeltrogna", r"Evangelisk Luthersk Mission|Bibeltrogna Vänner"),
    ("Laestadianer", r"laestadian|fridsfören", None, r"laestadian|fridsfören|fridsförbund", None),
    ("Adventistsamfundet", r"adventist", r"adventist", r"advent", r"Adventistsamfundet"),
    ("Trosrörelsen", r"trosrörelse|livets ord", r"livetsord", r"livets ord|trosgemenskap", r"Livets Ord"),
    ("Lutherska frikyrkor", r"lutherska bekännelse|lutherska konkordia|missionsprovinsen|evangelisk luthersk samling",
     r"lbk\.|missionsprovinsen", r"bekännelsekyrkan|konkordia", None),
]
FREE_CHURCH = "Andra frikyrkor"     # independent churches, or free churches whose denomination is unknown
ORDER = [f[0] for f in FAMILIES] + [FREE_CHURCH, "Övriga"]

GENERIC_MAIL = re.compile(
    r"(gmail|hotmail|live|outlook|yahoo|msn|icloud|me|mac|telia|tele2|spray|bredband2?|comhem|home|glocalnet|"
    r"bahnhof|passagen|swipnet|tiscali|algonet|telenor|proton|protonmail|gmx|aol|kyrktorget)\.")
FREE_CHURCH_NAME = re.compile(
    r"missions|frikyrk|betel|elim|betania|\bsion|smyrna|salem|filadelfia|kristet? center|kristna center|"
    r"vineyard|metodist|nazaren|evangelisk|baptist|gospel|hope|life|church|iglesia|igreja|biserica|"
    r"församlingen\b|kyrkans församling|korskyrkan|centrumkyrkan|hamnkyrkan|nybrokyrkan|slättenkyrkan", re.I)
NOT_FAMILY = re.compile(r"liberala katolska|lkk\.se|nyapostolisk|jehova|mormon|sista dagars", re.I)

SVK_NAME = re.compile(r"(församling(ar)?|pastorat)(\s*[-,(].*)?$|^svenska kyrkan|domkyrko|sjömanskyrkan", re.I)
NOT_SVK_NAME = re.compile(
    r"pingst|baptist|missions|katolsk|ortodox|advent|metodist|frikyrk|evangeli|equmenia|\befs\b|\belm\b|vineyard|"
    r"filadelfia|frälsnings|lutherska|nyapostolisk|koreansk|finsk|eritre|syrisk|armenisk|serbisk|allians|betania|"
    r"betel|elim|smyrna|\bsion|hope|united|international|nazaren|kristen gemenskap|livets ord|trosgemenskap|"
    r"apostolisk|församlingen\b|church|iglesia|igreja|biserica|kyrkan\b|kyrkans församling", re.I)


def classify(denomination: str | None, name: str | None, email: str | None, website: str | None,
             site_text: str | None = None) -> tuple[list[str], str]:
    """Return ([denominations], how it was decided)."""
    denom = denomination or ""
    if NOT_FAMILY.search(" ".join(filter(None, [denom, name, email, website]))):
        return ["Övriga"], "name"
    if denom:
        found = [label for label, d, _, _, _ in FAMILIES if re.search(d, denom, re.I)]
        if found:
            return found, "kyrktorget"
        if re.search(r"inget samfund|övriga", denom, re.I):
            return [FREE_CHURCH], "kyrktorget"

    domains = " ".join(x for x in [(email or "").split("@")[-1], website or ""] if x).lower()
    if domains:
        found = [label for label, _, dom, _, _ in FAMILIES if dom and re.search(dom, domains)]
        if found:
            return found[:1], "domain"

    n = name or ""
    found = [label for label, _, _, nm, _ in FAMILIES if nm and re.search(nm, n, re.I)]
    if found:
        return found[:1], "name"
    own_domain = bool(email and "@" in email and not GENERIC_MAIL.search(email.split("@")[-1].lower()))
    first_part = n.split(" - ")[0]
    if SVK_NAME.search(n) and not NOT_SVK_NAME.search(first_part) and not own_domain:
        return ["Svenska kyrkan"], "name"     # (Svenska kyrkan parishes use @svenskakyrkan.se, caught above)

    if site_text:
        counts = {}
        for label, _, _, _, txt in FAMILIES:
            if txt:
                c = len(re.findall(txt, site_text))
                if c:
                    counts[label] = c
        if counts:
            return [max(counts, key=counts.get)], "website"
    if FREE_CHURCH_NAME.search(n) or own_domain and re.search(r"kyrka|församling", n, re.I):
        return [FREE_CHURCH], "name"
    return ["Övriga"], "unknown"
