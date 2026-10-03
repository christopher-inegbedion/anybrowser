"""The task families.

Each one isolates a skill a browsing agent fails at in practice, and each draws
its answer from the seed, so no answer can be memorised across runs or guessed
from what a page "usually" says. A model that answers from priors instead of
reading the page -- one that says "done" on a page that says "landed" -- fails
here every time, at -1.

Pages are plain HTML with no external resources, so a run depends on nothing
but the engine and the agent.
"""

from __future__ import annotations

from html import escape
from random import Random

from .task import NOT_FOUND, Instance, Kind, Task

__all__ = ["ALL_TASKS", "task"]

_ADJECTIVES = (
    "amber", "brisk", "cobalt", "dusky", "ember", "fallow", "gilded", "hollow",
    "ivory", "jade", "keen", "lunar", "misty", "nimble", "ochre", "plum",
    "quiet", "russet", "silver", "tawny", "umber", "velvet", "willow", "zesty",
)  # fmt: skip
_NOUNS = (
    "anchor", "beacon", "candle", "drum", "easel", "flask", "globe", "harp",
    "inkwell", "jar", "kettle", "lantern", "mirror", "needle", "oar", "prism",
    "quill", "ribbon", "satchel", "teapot", "urn", "vase", "whistle", "yoke",
)  # fmt: skip
_FIRST = (
    "Ada", "Bram", "Cleo", "Dev", "Esme", "Femi", "Gus", "Hana", "Ivo", "Juno",
    "Kofi", "Lena", "Milo", "Nia", "Otto", "Priya", "Quinn", "Rosa", "Sami", "Tova",
)  # fmt: skip
_LAST = (
    "Achebe", "Brandt", "Castillo", "Dimitrov", "Eze", "Fischer", "Gallo", "Haddad",
    "Ivanova", "Jensen", "Kowalski", "Laine", "Moreau", "Nakamura", "Okafor", "Petrov",
)  # fmt: skip


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{escape(title)}</title>"
        "<style>body{font:16px system-ui;margin:2rem;max-width:44rem}"
        "td,th{padding:.3rem .8rem;text-align:left}"
        "button,input,select{font:inherit;padding:.3rem .6rem;margin:.2rem 0}"
        "</style></head><body>" + body + "</body></html>"
    )


def _code(rng: Random) -> str:
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    return f"{rng.choice(letters)}{rng.choice(letters)}-{rng.randint(1000, 9999)}"


def _price(rng: Random) -> str:
    # Never a round number of pence: "£48.20" read back as "48.2" would be
    # right and look wrong to a whole-token match.
    pence = rng.choice([p for p in range(1, 100) if p % 10])
    return f"£{rng.randint(3, 480)}.{pence:02d}"


def _products(rng: Random, count: int) -> list[tuple[str, str]]:
    names = rng.sample([f"{a.title()} {n}" for a in _ADJECTIVES for n in _NOUNS], count + 1)
    prices: set[str] = set()
    while len(prices) < count:
        prices.add(_price(rng))
    return list(zip(names[:count], rng.sample(sorted(prices), count), strict=True))


def _async_status(rng: Random) -> Instance:
    build = _code(rng)
    delay = rng.randint(1500, 2500)
    body = (
        "<h1>Deployment console</h1>"
        "<button id='go'>Deploy</button>"
        "<p id='status'>Status: idle</p>"
        "<script>document.getElementById('go').onclick=()=>{"
        "const s=document.getElementById('status');s.textContent='Status: deploying...';"
        f"setTimeout(()=>{{s.textContent='Status: deployed build {build}'}},{delay})}}"
        "</script>"
    )
    return Instance(
        goal="Click Deploy, and once the deploy has finished, report the build ID it shows.",
        pages={"index.html": _page("Deployment console", body)},
        expected=build,
    )


def _form_fill(rng: Random) -> Instance:
    first, last = rng.choice(_FIRST), rng.choice(_LAST)
    name = f"{first} {last}"
    email = f"{first.lower()}.{last.lower()}{rng.randint(10, 99)}@example.com"
    plan = rng.choice(["Starter", "Team", "Enterprise"])
    body = (
        "<h1>Create your account</h1>"
        "<form method='post' action='submit'>"
        "<p><label>Full name <input name='name'></label></p>"
        "<p><label>Email <input name='email' type='email'></label></p>"
        "<p><label>Plan <select name='plan'><option>Starter</option><option>Team</option>"
        "<option>Enterprise</option></select></label></p>"
        "<p><button type='submit'>Create account</button></p></form>"
    )
    return Instance(
        goal=f"Create an account for {name}, email {email}, on the {plan} plan.",
        pages={"index.html": _page("Sign up", body)},
        expected_fields={"name": name, "email": email, "plan": plan},
    )


def _long_list(rng: Random) -> Instance:
    products = _products(rng, 60)
    target, price = products[rng.randint(40, 57)]
    rows = "".join(f"<tr><td>{n}</td><td>{p}</td></tr>" for n, p in products)
    body = f"<h1>Catalogue</h1><table><tr><th>Item</th><th>Price</th></tr>{rows}</table>"
    return Instance(
        goal=f"What is the price of the {target}?",
        pages={"index.html": _page("Catalogue", body)},
        expected=price,
        decoys=tuple(p for _, p in products if p != price),
    )


def _pagination(rng: Random) -> Instance:
    pages_count, per_page = 5, 8
    orders = []
    codes: set[str] = set()
    while len(codes) < pages_count * per_page:
        codes.add(f"ORD-{rng.randint(10000, 99999)}")
    totals = [p for _, p in _products(rng, len(codes))]
    # Shuffle a sorted list, never a set: set order varies between processes
    # with hash randomisation, and the same seed must give the same task.
    shuffled = sorted(codes)
    rng.shuffle(shuffled)
    for code, total in zip(shuffled, totals, strict=True):
        orders.append((code, f"{rng.choice(_FIRST)} {rng.choice(_LAST)}", total))
    target_index = rng.randint(2 * per_page, pages_count * per_page - 1)
    target_code, _, target_total = orders[target_index]

    pages = {}
    for page in range(pages_count):
        chunk = orders[page * per_page : (page + 1) * per_page]
        rows = "".join(f"<tr><td>{c}</td><td>{n}</td><td>{t}</td></tr>" for c, n, t in chunk)
        nav = " ".join(
            f"<a href='page{i + 1}.html'>{i + 1}</a>" if i != page else f"<b>{i + 1}</b>"
            for i in range(pages_count)
        )
        if page < pages_count - 1:
            nav += f" <a href='page{page + 2}.html'>Next page</a>"
        body = (
            f"<h1>Orders</h1><table><tr><th>Order</th><th>Customer</th><th>Total</th></tr>"
            f"{rows}</table><p>Page {page + 1} of {pages_count}: {nav}</p>"
        )
        html = _page(f"Orders - page {page + 1}", body)
        pages[f"page{page + 1}.html"] = html
        if page == 0:
            pages["index.html"] = html
    return Instance(
        goal=f"What is the total for order {target_code}?",
        pages=pages,
        expected=target_total,
        decoys=tuple(t for _, _, t in orders if t != target_total),
    )


def _cookie_wall(rng: Random) -> Instance:
    email = f"{rng.choice(_FIRST).lower()}{rng.randint(100, 999)}@example.org"
    body = (
        "<h1>The Weekly Lantern</h1><p>Stories worth your Sunday morning.</p>"
        "<form method='post' action='submit'>"
        "<p><label>Email <input name='email' type='email'></label></p>"
        "<p><button type='submit'>Subscribe</button></p></form>"
        "<div id='wall' style='position:fixed;inset:0;background:rgba(0,0,0,.6);"
        "display:flex;align-items:center;justify-content:center'>"
        "<div style='background:#fff;padding:2rem;max-width:24rem'>"
        "<h2>We value your privacy</h2><p>We use cookies to run this site.</p>"
        "<button onclick=\"document.getElementById('wall').remove()\">Accept all</button> "
        "<button onclick=\"document.getElementById('wall').remove()\">Reject all</button>"
        "</div></div>"
    )
    return Instance(
        goal=f"Subscribe {email} to the newsletter.",
        pages={"index.html": _page("The Weekly Lantern", body)},
        expected_fields={"email": email},
    )


def _navigate_detail(rng: Random) -> Instance:
    people = rng.sample([f"{f} {last}" for f in _FIRST for last in _LAST], 8)
    extensions = [str(x) for x in rng.sample(range(2000, 7999), len(people))]
    target = rng.randrange(len(people))
    roles = ("Engineer", "Designer", "Analyst", "Manager", "Researcher")
    links = "".join(
        f"<li><a href='person{i}.html'>{escape(p)}</a></li>" for i, p in enumerate(people)
    )
    pages = {"index.html": _page("Staff directory", f"<h1>Staff directory</h1><ul>{links}</ul>")}
    for i, (person, ext) in enumerate(zip(people, extensions, strict=True)):
        body = (
            f"<h1>{escape(person)}</h1><p>Role: {rng.choice(roles)}</p>"
            f"<p>Office: {rng.randint(1, 6)}.{rng.randint(10, 40)}</p>"
            f"<p>Phone extension: {ext}</p><p><a href='index.html'>Back to directory</a></p>"
        )
        pages[f"person{i}.html"] = _page(person, body)
    return Instance(
        goal=f"What is {people[target]}'s phone extension?",
        pages=pages,
        expected=extensions[target],
        decoys=tuple(e for i, e in enumerate(extensions) if i != target),
    )


def _missing_item(rng: Random) -> Instance:
    products = _products(rng, 16)
    listed = {n for n, _ in products}
    absent = next(iter(listed))
    while absent in listed:
        absent = f"{rng.choice(_ADJECTIVES).title()} {rng.choice(_NOUNS)}"
    rows = "".join(f"<tr><td>{n}</td><td>{p}</td></tr>" for n, p in products)
    body = f"<h1>Catalogue</h1><table><tr><th>Item</th><th>Price</th></tr>{rows}</table>"
    return Instance(
        goal=(
            f"What is the price of the {absent}? If it is not listed, answer exactly {NOT_FOUND}."
        ),
        pages={"index.html": _page("Catalogue", body)},
        decoys=tuple(p for _, p in products),
    )


def _delayed_search(rng: Random) -> Instance:
    docs = [(f"{a.title()} {n} handbook", _code(rng)) for a, n in _distinct_pairs(rng, 20)]
    query_word = docs[rng.randrange(len(docs))][0].split()[1]
    # The same substring match the page uses, or the expected answer and the
    # page's first result could disagree.
    matches = [d for d in docs if query_word in d[0].lower()]
    first_code = matches[0][1]
    data = ",".join(f"['{t}','{c}']" for t, c in docs)
    delay = rng.randint(1200, 2000)
    body = (
        "<h1>Document search</h1>"
        "<form id='f'><input id='q' name='q' placeholder='Search documents'> "
        "<button type='submit'>Search</button></form><div id='out'></div>"
        f"<script>const D=[{data}];"
        "document.getElementById('f').onsubmit=(e)=>{e.preventDefault();"
        "const q=document.getElementById('q').value.trim().toLowerCase();"
        "const out=document.getElementById('out');out.innerHTML='<p>Searching...</p>';"
        "setTimeout(()=>{const hits=D.filter(d=>q&&d[0].toLowerCase().includes(q));"
        "out.innerHTML=hits.length?'<ol>'+hits.map(h=>'<li>'+h[0]+' (ref '+h[1]+')</li>')"
        f".join('')+'</ol>':'<p>No results.</p>'}},{delay})}};</script>"
    )
    return Instance(
        goal=(
            f"Search the documents for '{query_word}' and report the reference of the first result."
        ),
        pages={"index.html": _page("Document search", body)},
        expected=first_code,
        decoys=tuple(c for _, c in docs if c != first_code),
    )


# -- canvas ------------------------------------------------------------------ #
#
# Everything below is drawn on a <canvas>. The DOM holds one element with no
# text, so an agent that reads the page instead of looking at it has nothing to
# go on -- these are the tasks that need eyes. Clicks are hit-tested in page
# script against the same rectangles that were drawn, as a canvas game would.

_CANVAS_W, _CANVAS_H = 1000, 600


def _canvas_rects(rng: Random, count: int, w: int, h: int) -> list[tuple[int, int, int, int]]:
    rects: list[tuple[int, int, int, int]] = []
    while len(rects) < count:
        r = (rng.randint(30, _CANVAS_W - w - 30), rng.randint(90, _CANVAS_H - h - 30), w, h)
        if all(
            r[0] + w + 30 < o[0]
            or o[0] + o[2] + 30 < r[0]
            or r[1] + h + 30 < o[1]
            or o[1] + o[3] + 30 < r[1]
            for o in rects
        ):
            rects.append(r)
    return rects


def _canvas_page(title: str, script: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{escape(title)}</title>"
        "<style>body{margin:24px;background:#111}canvas{display:block}</style></head><body>"
        f"<canvas id='g' width='{_CANVAS_W}' height='{_CANVAS_H}'></canvas><script>"
        "const cv=document.getElementById('g'),c=cv.getContext('2d');"
        f"{script}</script></body></html>"
    )


def _canvas_menu(rng: Random) -> Instance:
    labels = rng.sample(["Play", "Settings", "Shop", "Profile", "Help", "Credits", "Quit"], 5)
    target = rng.choice(labels)
    rects = _canvas_rects(rng, len(labels), 170, 56)
    colors = ["#2b6cb0", "#2f855a", "#c05621", "#6b46c1", "#b83280"]
    buttons = ",".join(
        f"['{label}',{x},{y},{w},{h},'{color}']"
        for label, (x, y, w, h), color in zip(labels, rects, colors, strict=True)
    )
    script = (
        f"const B=[{buttons}];"
        "function draw(msg){c.fillStyle='#1a202c';c.fillRect(0,0,cv.width,cv.height);"
        "c.fillStyle='#e2e8f0';c.font='bold 34px sans-serif';c.textAlign='left';"
        "c.fillText(msg||'Main menu',30,55);"
        "for(const b of B){c.fillStyle=b[5];c.fillRect(b[1],b[2],b[3],b[4]);"
        "c.fillStyle='#fff';c.font='22px sans-serif';c.textAlign='center';c.textBaseline='middle';"
        "c.fillText(b[0],b[1]+b[3]/2,b[2]+b[4]/2);c.textBaseline='alphabetic'}}"
        "draw();cv.addEventListener('click',e=>{const r=cv.getBoundingClientRect(),"
        "x=e.clientX-r.left,y=e.clientY-r.top;"
        "const b=B.find(b=>x>=b[1]&&x<=b[1]+b[3]&&y>=b[2]&&y<=b[2]+b[4]);if(!b)return;"
        "draw('Opening '+b[0]+'...');"
        "fetch('submit',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},"
        "body:new URLSearchParams({choice:b[0]})})});"
    )
    return Instance(
        goal=f"Open the {target} menu.",
        pages={"index.html": _canvas_page("Game", script)},
        expected_fields={"choice": target},
    )


def _canvas_read(rng: Random) -> Instance:
    players = rng.sample([f"{f} {last[0]}." for f in _FIRST for last in _LAST], 6)
    scores = [str(s) for s in rng.sample(range(1200, 98000), len(players))]
    target = rng.randrange(len(players))
    rows = ",".join(f"['{p}','{s}']" for p, s in zip(players, scores, strict=True))
    script = (
        f"const R=[{rows}];c.fillStyle='#1a202c';c.fillRect(0,0,cv.width,cv.height);"
        "c.fillStyle='#f6e05e';c.font='bold 36px sans-serif';c.fillText('High scores',40,70);"
        "c.font='26px monospace';R.forEach((r,i)=>{c.fillStyle=i%2?'#e2e8f0':'#90cdf4';"
        "c.fillText(r[0],60,140+i*62);c.fillText(r[1],560,140+i*62)});"
    )
    return Instance(
        goal=f"What is {players[target]}'s high score?",
        pages={"index.html": _canvas_page("High scores", script)},
        expected=scores[target],
        decoys=tuple(s for i, s in enumerate(scores) if i != target),
    )


def _distinct_pairs(rng: Random, count: int) -> list[tuple[str, str]]:
    """Adjective-noun pairs with distinct nouns, so a one-word query has one hit
    family rather than matching half the corpus."""
    nouns = rng.sample(_NOUNS, count)
    return [(rng.choice(_ADJECTIVES), n) for n in nouns]


ALL_TASKS: tuple[Task, ...] = (
    Task("async-status", Kind.ANSWER, ("wait",), _async_status),
    Task("form-fill", Kind.STATE, ("type", "select", "submit"), _form_fill),
    Task("long-list", Kind.ANSWER, ("scroll", "read"), _long_list),
    Task("pagination", Kind.ANSWER, ("navigate", "read"), _pagination),
    Task("cookie-wall", Kind.STATE, ("obstruction", "type", "submit"), _cookie_wall),
    Task("navigate-detail", Kind.ANSWER, ("navigate", "read"), _navigate_detail),
    Task("missing-item", Kind.IMPOSSIBLE, ("honesty",), _missing_item),
    Task("delayed-search", Kind.ANSWER, ("type", "wait"), _delayed_search),
    Task("canvas-menu", Kind.STATE, ("vision", "click"), _canvas_menu),
    Task("canvas-read", Kind.ANSWER, ("vision", "read"), _canvas_read),
)


def task(task_id: str) -> Task:
    for t in ALL_TASKS:
        if t.id == task_id:
            return t
    known = ", ".join(t.id for t in ALL_TASKS)
    raise KeyError(f"no task {task_id!r}; known: {known}")
