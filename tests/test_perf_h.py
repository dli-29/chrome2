"""Performance unit H: the declarativeNetRequest engine. Rules share their frozensets and keep no action dict they
never read; a request is only tested against rules filed under keys it has, site-specific rules only from that site,
and a urlFilter's literal is looked for before its regular expression runs. Matching results must stay identical:
a brute-force reference checks the index on random rules and requests."""
from __future__ import annotations

import gc
import json
import random
import tracemalloc

import pytest
from PyQt6.QtCore import QByteArray, QObject, QUrl

from test_extension_review import simple_ext

PAGE = QUrl("https://site.example/")


def extension(fg, rules, ruleset="set"):
    return fg.NetExtension("e" * 32, [fg.RuleIndex(rules, ruleset)], ["<all_urls>"], False)


def request(fg, net, url, kind="script", initiator=PAGE, page=PAGE, method="get", tab=None):
    return net._request(QUrl(url), kind, method, initiator, page, tab)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  The index is lean: shared sets, no action dict for block/allow rules
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_rule_index_is_lean(fg):
    raw = [{"id": i, "action": {"type": "block"}, "condition": {"urlFilter": f"||ads{i}.example^", "resourceTypes": ["script", "image"]}}
           for i in range(20000)]
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        index = fg.RuleIndex(raw, "r")
        gc.collect()
        retained = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
    assert retained / len(raw) < 800, retained / len(raw)  # about 1.6 kB per rule before
    a, b = index.keyed["d:ads1.example"][0], index.keyed["d:ads2.example"][0]
    assert a.types is b.types and a.types == {"script", "image"}
    assert a.not_domains is a.not_initiators is a.not_methods is a.not_tabs is fg._NO_NAMES
    assert a.action is None and a.kind == "block"
    redirect = fg.NetRule({"id": 1, "action": {"type": "redirect", "redirect": {"url": "https://r/"}}, "condition": {"urlFilter": "x"}}, "r")
    assert redirect.action == {"type": "redirect", "redirect": {"url": "https://r/"}}
    headers = fg.NetRule({"id": 2, "action": {"type": "modifyHeaders", "requestHeaders": [{"header": "a", "operation": "set", "value": "b"}]},
                          "condition": {"urlFilter": "x"}}, "r")
    assert headers.action["requestHeaders"][0]["header"] == "a"


@pytest.mark.parametrize("kind, cond, field, expected", [
    ("block", {"resourceTypes": []}, "types", "all-but-main"),
    ("block", {"resourceTypes": "script"}, "types", "all-but-main"),
    ("block", {"resourceTypes": ["Script", 3, "bogus"]}, "types", {"script"}),
    ("block", {"resourceTypes": ["bogus"]}, "types", frozenset()),  # (unmatchable, as before)
    ("block", {"excludedResourceTypes": ["image", "nope"]}, "types", "all-but-image"),
    ("allowAllRequests", {"excludedResourceTypes": "x"}, "types", {"sub_frame"}),
    ("allowAllRequests", {"resourceTypes": ["main_frame", "script"]}, "types", {"main_frame"}),
    ("block", {"domains": ["A.com"]}, "initiators", {"a.com"}),
    ("block", {"initiatorDomains": None, "domains": ["a.com"]}, "initiators", None),  # present but null: no alias
    ("block", {"initiatorDomains": ["b.com"], "domains": ["a.com"]}, "initiators", {"b.com"}),
    ("block", {"excludedDomains": ["X.com", 1]}, "not_initiators", {"x.com"}),
    ("block", {"excludedInitiatorDomains": [], "excludedDomains": ["x.com"]}, "not_initiators", frozenset()),
    ("block", {"excludedRequestDomains": []}, "not_domains", frozenset()),
    ("block", {"requestDomains": []}, "domains", frozenset()),
    ("block", {"requestDomains": "a.com"}, "domains", None),
    ("block", {"requestMethods": ["POST"]}, "methods", {"post"}),
    ("block", {"excludedRequestMethods": None}, "not_methods", frozenset()),
    ("block", {"excludedTabIds": [1, "2"]}, "not_tabs", {1}),
    ("block", {"tabIds": []}, "tabs", frozenset()),
    ("block", {"domainType": "firstParty"}, "party", "firstParty"),
    ("block", {"domainType": "both"}, "party", None),
    ("block", {"regexFilter": "", "urlFilter": "x"}, "text", ""),  # an empty regexFilter: a regex rule without a pattern
    ("block", {"regexFilter": None, "urlFilter": "x"}, "text", "x"),
    ("block", {"urlFilter": "||a.com^"}, "action", None),
    ("upgradeScheme", {"urlFilter": "||a.com^"}, "action", None)])
def test_rule_fields_from_odd_inputs(fg, kind, cond, field, expected):
    action = {"type": kind, "redirect": {"url": "https://r/"}} if kind == "redirect" else {"type": kind}
    rule = fg.NetRule({"id": 1, "action": action, "condition": cond}, "r")
    if expected == "all-but-main":
        expected = fg.DNR_TYPES - {"main_frame"}
    elif expected == "all-but-image":
        expected = fg.DNR_TYPES - {"image"}
    assert getattr(rule, field) == expected
    if field == "types":
        assert rule.types is fg._TYPE_SETS[rule.types]  # interned: shared with every rule that has the same set
    if field == "initiators" and expected is None:
        assert rule.keys == []


@pytest.mark.parametrize("pattern, case, need", [
    ("||example.com^", False, "example.com"), ("||example.com/Path*x", False, "example.com/path"), ("|https://*", False, "https://"),
    ("*", False, ""), ("^ad^*|", False, "ad"), ("/ads/*banner", False, "banner"), ("AD", True, "AD"), ("AD", False, "ad"),
    ("||", False, ""), ("|", False, ""), ("a|b", False, "a"), ("|||abc|", False, "abc"), ("ads/ü", False, ""),
    ("/zz*ü/longer", False, "/zz")])
def test_url_filter_literal(fg, pattern, case, need):
    assert fg._literal(pattern, case) == need == fg.UrlFilter(pattern, case).need


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Matching is unchanged: every pattern through UrlFilter and through NetRule.matches (with its prefilter)
# ══════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("pattern, url, case, hit", [
    ("||example.com^", "https://example.com/x", False, True), ("||example.com^", "https://a.example.com:8080/", False, True),
    ("||example.com^", "https://notexample.com/", False, False), ("||example.com^", "https://example.com.evil.net/", False, False),
    ("|https://a.com/x|", "https://a.com/x", False, True), ("|https://a.com/x|", "https://a.com/xy", False, False),
    ("/ads/*banner", "http://h/ads/big-banner.png", False, True), ("^ad^", "http://h/x/ad/y", False, True),
    ("^ad^", "http://h/x/adx", False, False), ("^ad^", "http://h/x/ad", False, True), ("ADS", "http://h/ads", False, True),
    ("ads", "http://h/ADS/x", False, True), ("ads", "http://h/ADS/x", True, False), ("ADS", "http://h/ADS/x", True, True),
    ("AD", "http://h/ad", True, False), ("AD", "http://h/AD", True, True), ("*", "http://h/", False, True),
    ("^ad^*|", "http://h/ad", False, True), ("|https://*", "https://h/", False, True), ("|https://*", "http://h/", False, False),
    ("||", "http://h/", False, True), ("|", "http://h/", False, True), ("a*b*c", "http://h/acb", False, False),
    ("a*b*c", "http://h/abbc", False, True), ("||b.com/x|", "https://b.com/x", False, True), ("||b.com/x|", "https://b.com/xy", False, False),
    ("|http*://*.js|", "http://h/a.js", False, True), ("ad^x", "http://h/ad", False, False), ("ad^^", "http://h/ad", False, True),
    ("/a%20b", "http://h/a%20b", False, True), ("/a b", "http://h/a%20b", False, False), ("x.js|", "http://h/x.js?x", False, False)])
def test_url_filters_through_rules(fg, pattern, url, case, hit):
    assert fg.UrlFilter(pattern, case).search(url) is hit
    rule = fg.NetRule({"id": 1, "action": {"type": "block"}, "condition": {"urlFilter": pattern, "isUrlFilterCaseSensitive": case}}, "r")
    assert rule.matches(request(fg, fg.NetRules(None), url, "image")) is hit


def test_rule_index_keys_unchanged(fg):
    for pattern, keys in (("||example.com^", ["d:example.com"]), ("||example.com/path", ["d:example.com"]), ("||example.com", ["t:example"]),
                          ("/ads/*banner", ["t:ads"]), ("|https://*", []), ("ads", []), ("||go.*.com/smartpop/", ["t:smartpop"]),
                          ("&adslot=", ["t:adslot"]), ("||EXAMPLE.com^", ["d:example.com"]), ("|ads|", ["t:ads"])):
        assert fg._filter_keys(pattern) == keys, pattern


# ══════════════════════════════════════════════════════════════════════════════════════════
#  The index finds exactly what a scan of every rule finds
# ══════════════════════════════════════════════════════════════════════════════════════════
def random_rules(rng, count: int) -> list[dict]:
    domain = lambda: f"d{rng.randrange(80)}.com"
    site = lambda: f"s{rng.randrange(25)}.com"
    word = lambda: f"word{rng.randrange(80)}"
    rules = []
    for i in range(count):
        cond: dict = {}
        shape = rng.randrange(9)
        kind = rng.choices(["block", "allow", "redirect", "modifyHeaders", "allowAllRequests", "upgradeScheme"], [25, 25, 5, 25, 4, 5])[0]
        if kind == "allowAllRequests" and shape not in (0, 1, 6):
            kind = "allow"  # (a page-wide allow for every page would hide everything else)
        action = {"type": kind}
        if kind == "redirect":
            action["redirect"] = {"url": "https://r.example/"}
        elif kind == "modifyHeaders":
            action["requestHeaders"] = [{"header": "x-h", "operation": "append", "value": "v"}]
        if shape == 0:
            cond["urlFilter"] = f"||{domain()}^"
        elif shape == 1:
            cond["urlFilter"] = f"||{domain()}/{word()}"
        elif shape == 2:
            cond["urlFilter"] = f"/{word()}*x"  # generic, with a literal
        elif shape == 3:
            cond["urlFilter"] = f"/{word()}/"
        elif shape == 4:
            cond["regexFilter"] = rf"{word()}\d*/x"  # generic, no literal
        elif shape == 5:
            cond["urlFilter"] = rng.choice(["^ad^*|", "a|b", "/WORD1/", "WORD2", "||d1.com*word2", "|https://sub.*/x"])
        elif shape == 6:
            d = domain()
            cond["requestDomains"] = [d, "sub." + d] if rng.random() < 0.5 else [d]
            if rng.random() < 0.5:
                cond["urlFilter"] = f"/{word()}"
        elif shape == 7:
            cond["requestDomains"] = [domain(), domain()]  # no pattern at all
        else:
            cond["initiatorDomains"], cond["resourceTypes"] = [site()], rng.sample(sorted(fg_types), 2)  # i: keys only
        if shape == 0 and rng.random() < 0.05:
            cond["tabIds"] = [3]
        if rng.random() < 0.4:
            s = site()
            cond[rng.choice(["initiatorDomains", "domains"])] = [s, "a." + s] if rng.random() < 0.3 else [s] if rng.random() < 0.8 else [s, site()]
        if rng.random() < 0.1:
            cond["excludedInitiatorDomains"] = [site()]
        if rng.random() < 0.1:
            cond["excludedRequestDomains"] = [domain()]
        if rng.random() < 0.3:
            cond["resourceTypes"] = rng.sample(sorted(fg_types), rng.randint(1, 4))
        elif rng.random() < 0.1:
            cond["excludedResourceTypes"] = rng.sample(sorted(fg_types), 2)
        if kind == "allowAllRequests" and "resourceTypes" not in cond:
            cond["resourceTypes"] = ["main_frame"]
        if rng.random() < 0.1:
            cond["domainType"] = rng.choice(["firstParty", "thirdParty"])
        if rng.random() < 0.1:
            cond["requestMethods"] = ["post"]
        if rng.random() < 0.05:
            cond["isUrlFilterCaseSensitive"] = True
        rules.append({"id": i + 1, "priority": rng.randint(1, 3), "action": action, "condition": cond})
    return rules


fg_types = ("main_frame", "sub_frame", "script", "image", "xmlhttprequest", "stylesheet", "font", "media", "ping", "other")


def random_request(fg, net, rng):
    host = rng.choice([f"d{rng.randrange(80)}.com", f"sub.d{rng.randrange(80)}.com", f"x.sub.d{rng.randrange(80)}.com", "other.example"])
    parts = [rng.choice([f"word{rng.randrange(80)}", f"word{rng.randrange(80)}x", "x", "WORD1", "ad"]) for _ in range(rng.randint(0, 3))]
    path = "/" + "/".join(parts) + rng.choice(["", "/", "/x", "?a=word1&word2=1"])
    url = f"{rng.choice(['http', 'https'])}://{host}{path}"
    source = rng.choice([f"s{rng.randrange(25)}.com", f"a.s{rng.randrange(25)}.com", host, f"d{rng.randrange(80)}.com", ""])
    initiator = QUrl(f"https://{source}/") if source else QUrl()
    page = QUrl(f"https://{source or host}/{rng.choice(['', 'word3/x', 'word5'])}")
    kind = rng.choice(fg_types)
    return net._request(QUrl(url), kind, rng.choice(["get", "post"]), initiator, page, None), page


def reference_decide(fg, net, rule_lists, req, page):
    """decide() with every rule tested (nothing filed away): what the index must give."""
    best, headers = None, {}
    for rules in rule_lists:
        for rule in rules:
            hit = rule.matches(req)
            if hit is None:
                return None
            if hit and rule.kind == "modifyHeaders":
                headers[rule.id] = rule
            elif hit and (best is None or (rule.priority, -rule.rank) > (best.priority, -best.rank)):
                best = rule
    headers = list(headers.values())
    if req.type not in ("main_frame", "sub_frame") and page.isValid() and any(r.kind == "allowAllRequests" for rs in rule_lists for r in rs):
        page_req = net._request(page, "main_frame", "get", QUrl(), page, -1)
        hits = [r.priority for rs in rule_lists for r in rs if r.kind == "allowAllRequests" and r.matches(page_req)]
        allowed = max(hits) if hits else None
        if allowed is not None and (best is None or best.priority <= allowed):
            return None, [h for h in headers if h.priority > allowed]
    if best is not None and best.kind in ("block", "redirect", "upgradeScheme"):
        return best, []
    return best, [h for h in headers if best is None or h.priority > best.priority]


def test_index_finds_what_a_full_scan_finds(fg):
    rng = random.Random(20261008)
    raw = [random_rules(rng, 700) for _ in range(3)]
    for n, rules in enumerate(raw):  # ids unique across the rule sets
        for r in rules:
            r["id"] += n * 10000
    indexes = [fg.RuleIndex(rules, f"set{n}") for n, rules in enumerate(raw)]
    assert sum(len(i.generic) for i in indexes) > 100 and sum(len(i.by_initiator) for i in indexes) > 100
    ext = fg.NetExtension("e" * 32, indexes, ["<all_urls>"], False)
    reference = [[fg.NetRule(r, f"set{n}") for r in rules] for n, rules in enumerate(raw)]  # fresh objects, no index
    net, ref_net = fg.NetRules(None), fg.NetRules(None)
    outcomes = {"none": 0, "hit": 0, "headers": 0, "allowed": 0}
    for _ in range(3000):
        req, page = random_request(fg, net, rng)
        got, want = net.decide(ext, req, page), reference_decide(fg, ref_net, reference, req, page)
        if want is None:
            assert got is None, req.url
            outcomes["none"] += 1
            continue
        assert got is not None, req.url
        best, headers = got
        ref_best, ref_headers = want
        assert sorted(h.id for h in headers) == sorted(h.id for h in ref_headers), req.url
        outcomes["headers"] += bool(headers)
        if ref_best is None:
            assert best is None, (req.url, best.id)
            outcomes["allowed"] += any(r.matches(req) and r.kind != "modifyHeaders" for rs in reference for r in rs)  # the page's allowAllRequests won
            continue
        assert best is not None, (req.url, ref_best.id)
        assert (best.priority, best.rank, best.kind) == (ref_best.priority, ref_best.rank, ref_best.kind), req.url
        matching = {r.id for rs in reference for r in rs if r.matches(req) and (r.priority, r.rank) == (best.priority, best.rank)}
        assert best.id in matching, req.url  # (among equals the index may pick another: same effect)
        outcomes["hit"] += 1
    assert outcomes["hit"] > 500 and outcomes["headers"] > 20 and outcomes["none"] > 0 and outcomes["allowed"] > 0, outcomes


def test_header_rule_applies_once(fg):
    """A rule filed under two of a request's keys came back twice: an "append" header was appended twice."""
    rule = {"id": 1, "action": {"type": "modifyHeaders", "requestHeaders": [{"header": "X-Test", "operation": "append", "value": "v"}]},
            "condition": {"requestDomains": ["a.com", "b.a.com"]}}
    net, page = fg.NetRules(None), QUrl("https://x.b.a.com/")
    req = request(fg, net, "https://x.b.a.com/p.js", page=page)
    best, headers = net.decide(extension(fg, [rule]), req, page)
    assert best is None and [h.id for h in headers] == [1]

    class Info:  # what NetRules.apply sees of a request
        def __init__(self):
            self.set: list[tuple[bytes, bytes]] = []
        requestUrl = lambda self: QUrl("https://x.b.a.com/p.js")
        resourceType = lambda self: fg._RT.ResourceTypeScript
        initiator = firstPartyUrl = lambda self: QUrl("https://x.b.a.com/")
        requestMethod = lambda self: QByteArray(b"GET")
        httpHeaders = lambda self: {QByteArray(b"X-Test"): QByteArray(b"orig")}
        setHttpHeader = lambda self, k, v: self.set.append((bytes(k), bytes(v)))
        block = redirect = lambda self, *_: None

    net.c, net._exts = QObject(), [extension(fg, [rule])]
    info = Info()
    net.apply(info, None)
    assert info.set == [(b"X-Test", b"orig, v")]


def test_site_exceptions_not_tested_elsewhere(fg, monkeypatch):
    """300 per-site exceptions for one domain: a request from one site is tested against its own, not all 300."""
    rules = [{"id": i + 1, "action": {"type": "allow"}, "condition": {"urlFilter": "||gtm.example/x", "initiatorDomains": [f"site{i}.com"]}}
             for i in range(300)]
    ext, net = extension(fg, rules), fg.NetRules(None)
    assert not ext.indexes[0].keyed and set(ext.indexes[0].by_initiator) == {"d:gtm.example"}
    tested = []
    matches = fg.NetRule.matches
    monkeypatch.setattr(fg.NetRule, "matches", lambda self, req: tested.append(self.id) or matches(self, req))
    page = QUrl("https://site5.com/")
    best, headers = net.decide(ext, request(fg, net, "https://gtm.example/x.js", initiator=page, page=page), page)
    assert best is not None and best.id == 6 and tested == [6]
    other = QUrl("https://elsewhere.example/")
    assert net.decide(ext, request(fg, net, "https://gtm.example/x.js", initiator=other, page=other), other) == (None, []) and tested == [6]


def test_generic_rules_prefiltered(fg, monkeypatch):
    """Rules no key files (open-ended words) run no regular expression on a URL without their literal."""
    rules = [{"id": i + 1, "action": {"type": "block"}, "condition": {"urlFilter": f"/zzword{i}x"}} for i in range(500)]
    ext, net = extension(fg, rules), fg.NetRules(None)
    index = ext.indexes[0]
    assert len(index.generic) == 500 and not index.keyed and index.generic[7] == ("/zzword7x", index.generic[7][1])
    searched = []
    search = fg.UrlFilter.search
    monkeypatch.setattr(fg.UrlFilter, "search", lambda self, url, cut=False: searched.append(url) or search(self, url, cut))
    assert net.decide(ext, request(fg, net, "https://h.example/nothing/here.js"), PAGE) == (None, []) and searched == []
    best, _ = net.decide(ext, request(fg, net, "https://h.example/zzword7x.js"), PAGE)
    assert best is not None and best.id == 8 and len(searched) == 1
    best, _ = net.decide(ext, request(fg, net, "https://h.example/ZZWORD7X.js"), PAGE)  # (case-insensitive: still found)
    assert best is not None and best.id == 8 and len(searched) == 2


def test_only_present_keys_are_looked_up(fg, monkeypatch):
    """A URL's words aren't looked up in every index: keys no index files a rule under are dropped once."""
    rules = [{"id": 1, "action": {"type": "block"}, "condition": {"urlFilter": "/ads/"}}]
    ext, net = extension(fg, rules), fg.NetRules(None)
    assert ext.present == {"t:ads"} and ext.allow_all is False
    seen = []
    candidates = fg.RuleIndex.candidates
    monkeypatch.setattr(fg.RuleIndex, "candidates", lambda self, req, keys: seen.append(list(keys)) or candidates(self, req, keys))
    best, _ = net.decide(ext, request(fg, net, "https://cdn.example/ads/x.js?a=b&c=d"), PAGE)
    assert best is not None and best.id == 1 and seen == [["t:ads"]]
    ext = extension(fg, [{"id": 2, "action": {"type": "allowAllRequests"}, "condition": {"urlFilter": "||site.example^", "resourceTypes": ["main_frame"]}}])
    assert ext.allow_all is True


# ══════════════════════════════════════════════════════════════════════════════════════════
#  A rule set of site-specific rules only (filed per initiator) is still enforced
# ══════════════════════════════════════════════════════════════════════════════════════════
SITE_RULE = {"id": 1, "action": {"type": "block"}, "condition": {"urlFilter": "/extra.png", "initiatorDomains": ["127.0.0.1"]}}


def test_initiator_only_index_is_kept(fg):
    index = fg.RuleIndex([SITE_RULE], "r")
    assert not index.keyed and not index.generic and list(index.by_initiator) == ["t:extra"]
    ext, net = fg.NetExtension("e" * 32, [index], ["<all_urls>"], False), fg.NetRules(None)
    assert ext.present == {"t:extra"}
    page = QUrl("http://127.0.0.1/")
    best, _ = net.decide(ext, request(fg, net, "http://127.0.0.1/extra.png?1", "image", page, page), page)
    assert best is not None and best.id == 1
    other = QUrl("http://other.example/")
    assert net.decide(ext, request(fg, net, "http://127.0.0.1/extra.png?1", "image", other, other), other) == (None, [])


def test_initiator_only_ruleset_is_enforced(window, harness, server, tmp_path):
    from test_extension_hardening import DNR_PAGE, IMAGES, dnr_page
    server.add("/dnr", DNR_PAGE, "text/html; charset=utf-8")
    entry = harness.install_ok(simple_ext(tmp_path / "s", "Sitewise", {"rules/only.json": json.dumps([SITE_RULE])},
                                          permissions=["declarativeNetRequest"],
                                          declarative_net_request={"rule_resources": [{"id": "only", "enabled": True, "path": "rules/only.json"}]}),
                               "Sitewise")
    tab = window.current_tab()
    assert dnr_page(tab, server) == set(IMAGES) - {"/extra.png"}
    indexes = [i for e in harness.controller.net.extensions() if e.id == entry.id for i in e.indexes]
    assert [sorted(i.by_initiator) for i in indexes] == [["t:extra"]] and not indexes[0].keyed  # kept, with nothing else filed
