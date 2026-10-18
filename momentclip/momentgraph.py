"""The moment graph -- Graphify as the brain.

We build a graph of candidate moments and the things they connect to, then ask
structural questions that a transcript-similarity model cannot ask:

  * which moments are **god nodes**?  Highest degree == the recurring bits,
    catchphrases and running jokes. Nobody else surfaces these.
  * which moments are **surprising**?  A moment that links two otherwise
    unrelated parts of the episode, or bridges two clusters.
  * where do **communities** sit?  Leiden clusters are the episode's natural arcs,
    so we can cut on community boundaries and know adjacent clips won't overlap.
  * which moments are **echoes**?  Near-duplicate content that should not both
    make the final cut.

Every edge is tagged EXTRACTED (stated by the source) or INFERRED (derived by
resolution) -- the same confidence contract Graphify uses for code. That tag is
surfaced in the UI so a creator can see which reasoning was read vs deduced.

Graphify is used when installed. When it is not, the identical graph schema and
the identical structural features are computed with networkx directly, so the
product never depends on it being present.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict

from .candidates import Moment

log = logging.getLogger("momentclip.momentgraph")

try:  # networkx is a hard dependency; graphify is optional
    import networkx as nx
except ImportError:  # pragma: no cover
    nx = None  # type: ignore

_WORD_RE = re.compile(r"[A-Za-z0-9']+")

# Words too generic to be graph-meaningful.
STOPWORDS = {
    "the", "and", "that", "have", "for", "not", "with", "you", "this", "but",
    "his", "from", "they", "she", "will", "one", "all", "would", "there",
    "their", "what", "out", "about", "who", "get", "which", "when", "make",
    "can", "like", "just", "know", "take", "into", "your", "some", "them",
    "then", "how", "its", "our", "also", "back", "any", "these", "give",
    "day", "most", "was", "were", "been", "has", "had", "did", "does",
    "going", "really", "thing", "things", "yeah", "okay", "right", "well",
    "like", "know", "mean", "think", "want", "say", "said", "good", "gonna",
    "lot", "bit", "actually", "maybe", "sure", "okay",
}


def _content_terms(text: str, limit: int = 12) -> list[str]:
    toks = [w.lower() for w in _WORD_RE.findall(text or "")]
    counts = Counter(t for t in toks if len(t) > 3 and t not in STOPWORDS)
    return [w for w, _ in counts.most_common(limit)]


def graphify_available() -> bool:
    """Whether the optional Graphify backend is importable.

    Graphify is not published on PyPI, so in practice this is always False
    and the networkx backend below does the work. The hook is kept because it
    is a clean seam for anyone who vendors it internally -- but nothing in
    this repository depends on it, and /api/health reports the backend that
    actually ran rather than the one that was hoped for.
    """
    try:
        import graphify  # noqa: F401  (presence is the check)
        return True
    except ImportError:
        return False


# Which clustering implementation _cluster() used last. Health reports this,
# so the docs cannot claim Leiden while the code is quietly doing something
# weaker. See _cluster() for the order.
CLUSTER_BACKEND = "unknown"


def _cluster(G):
    """Community detection, best available first.

    Order matters, and it is: Leiden > Louvain > greedy modularity. The old
    version of this function tried Louvain and then greedy modularity, while
    the module docstring promised Leiden -- so with no louvain installed the
    claim was false and nothing reported it.
    """
    global CLUSTER_BACKEND

    nodes = list(G.nodes())
    if len(nodes) < 2:
        CLUSTER_BACKEND = "trivial"
        return {n: 0 for n in nodes}

    # 1. Leiden, if leidenalg is installed. This is the documented behaviour.
    try:
        import igraph as ig  # type: ignore
        import leidenalg  # type: ignore

        edges = [(u, v) for u, v in G.edges() if u != v]
        graph = ig.Graph(n=len(nodes), edges=list(edges), directed=False)
        part = leidenalg.find_partition(graph, leidenalg.ModularityVertexPartition)
        out = {nodes[idx]: int(cid) for idx, cid in enumerate(part.membership)}
        if out:
            CLUSTER_BACKEND = "leidenalg"
            return out
    except Exception as exc:  # noqa: BLE001
        log.debug("leidenalg unavailable (%s), trying Louvain", exc)

    # 2. Louvain
    try:
        import community as community_louvain  # type: ignore
        CLUSTER_BACKEND = "louvain"
        return community_louvain.best_partition(G)
    except Exception:
        pass

    # 3. Greedy modularity. Weaker than Leiden, but it ships with networkx,
    #    so there is always an answer.
    import networkx.algorithms.community as nxc
    CLUSTER_BACKEND = "greedy-modularity"
    return {n: i for i, c in enumerate(nxc.greedy_modularity_communities(G))
            for n in c}


def _surprising_pairs(G, threshold: float = 0.0) -> list[tuple[str, str]]:
    """An edge whose endpoints sit in different communities.

    Graphify's surprising_connections is tried first if vendored in, but the
    answer this actually returns is the cross-community edge set computed
    here, which is the part that is actually load-bearing.
    """
    try:
        from graphify.analyze import surprising_connections  # type: ignore
        pairs = set()
        for item in surprising_connections(G) or []:
            u, v = (item if isinstance(item, (tuple, list)) else (None, None))
            if u and v and u in G and v in G:
                pairs.add((u, v))
        if pairs:
            return sorted(pairs)
    except Exception as exc:  # noqa: BLE001
        log.debug("graphify surprising_connections unavailable (%s)", exc)

    # fallback: an edge whose endpoints sit in different dense regions
    try:
        comm = _cluster(G)
    except Exception:
        return []
    pairs = []
    for u, v in G.edges():
        if comm.get(u) is not None and comm.get(u) != comm.get(v):
            pairs.append((u, v))
    return sorted(pairs)


# --------------------------------------------------------------------------
def build(moments: list[Moment], graph_of_history: "object | None" = None):
    """Assemble the moment graph.

    Node kinds:  moment | term | segment
    Edge kinds:  precedes | shares_term | echoes
    """
    if nx is None:
        raise RuntimeError("networkx is required: pip install networkx")

    G = nx.Graph()
    terms_by_moment: dict[str, list[str]] = {}

    for m in moments:
        G.add_node(m.id, kind="moment", start=m.start, end=m.end,
                   text=(m.text or "")[:400], tag=m.tag, source=m.source)
        terms = _content_terms(m.text)
        terms_by_moment[m.id] = terms
        for t in terms:
            G.add_node(f"t::{t}", kind="term", label=t)
            G.add_edge(m.id, f"t::{t}", relation="shares_term",
                       confidence="EXTRACTED")

    # consecutive moments: a cheap narrative spine
    for a, b in zip(moments, moments[1:]):
        overlap = len(set(terms_by_moment.get(a.id, [])) &
                      set(terms_by_moment.get(b.id, [])))
        G.add_edge(a.id, b.id, relation="precedes",
                   confidence="EXTRACTED", weight=1 + overlap)

    # lexical echo: Jaccard on content terms -> near-duplicate detection
    sets = {m.id: set(terms_by_moment.get(m.id, [])) for m in moments}
    ids = [m.id for m in moments]
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            sa, sb = sets[a], sets[b]
            if not sa or not sb:
                continue
            jac = len(sa & sb) / len(sa | sb)
            if jac >= 0.6:
                G.add_edge(a, b, relation="echoes", confidence="INFERRED",
                           weight=float(jac))

    return G


def annotate(moments: list[Moment], G, top_n: int = 10) -> None:
    """Compute structural features and write them onto each Moment."""
    degree = dict(G.degree())
    clusters = _cluster(G)
    surprising = set(_surprising_pairs(G))

    # god nodes: moments far above the mean degree of moment nodes
    moment_degrees = [degree.get(m.id, 0) for m in moments]
    mean_deg = (sum(moment_degrees) / len(moment_degrees)) if moment_degrees else 0.0
    cutoff = mean_deg * 1.35 + 1.0

    degree_norm = {}
    if moment_degrees:
        dmin, dmax = min(moment_degrees), max(moment_degrees)
        rng = max(1e-9, dmax - dmin)
        degree_norm = {m.id: (degree.get(m.id, 0) - dmin) / rng for m in moments}

    for m in moments:
        deg = degree.get(m.id, 0)
        comm = clusters.get(m.id)
        comm_size = sum(1 for c in clusters.values() if c == comm)
        reasons: list[str] = []

        centrality = float(degree_norm.get(m.id, 0.0))
        if deg >= cutoff and deg > 0:
            reasons.append(f"god node (links {deg} threads)")
        elif centrality > 0.6:
            reasons.append(f"highly connected ({deg} threads)")

        surprise = 1.0 if any(m.id in p for p in surprising) else 0.0
        if surprise:
            reasons.append("bridges unrelated parts of the episode")

        # how isolated is this moment's community?
        n_clusters = max(1, len(set(clusters.values())))
        arc_spread = min(1.0, (comm_size / max(1, len(moments))) * 1.5)

        # lexical novelty against the rest of the episode
        mine = set(_content_terms(m.text))
        others = Counter()
        for other in moments:
            if other.id == m.id:
                continue
            for t in _content_terms(other.text):
                others[t] += 1
        if mine:
            rarity = [others.get(t, 0) for t in mine]
            novelty = 1.0 - (sum(rarity) / len(rarity) / max(1, len(moments)))
        else:
            novelty = 0.5
        novelty = float(max(0.0, min(1.0, novelty)))

        echo = sum(1 for n in G.neighbors(m.id)
                   if G.edges[m.id, n].get("relation") == "echoes")
        if echo:
            reasons.append(f"{echo} near-duplicate moment(s)")

        m.node = {
            "id": m.id, "kind": "moment", "degree": deg,
            "cluster": comm, "cluster_size": comm_size,
            "n_clusters": n_clusters,
        }
        m.graph_score = float(min(1.0, 0.45 * centrality + 0.30 * surprise
                                  + 0.15 * novelty + 0.10 * arc_spread))
        m.features["graph_centrality"] = centrality
        m.features["graph_surprise"] = surprise
        m.features["lexical_novelty"] = novelty
        m.features["echo_degree"] = float(echo)
        m.graph_reasons = reasons

    log.info("graph: %d nodes, %d edges, %d communities",
             G.number_of_nodes(), G.number_of_edges(),
             len(set(clusters.values())))


def communities(moments: list[Moment], G) -> dict[int, list[str]]:
    clusters = _cluster(G)
    out: dict[int, list[str]] = defaultdict(list)
    for m in moments:
        out[clusters.get(m.id, 0)].append(m.id)
    return dict(out)


def summarise(moments: list[Moment], G) -> dict:
    """A small graph report, in the spirit of GRAPH_REPORT.md."""
    degree = dict(G.degree())
    by_id = {m.id: m for m in moments}
    moment_nodes = [n for n, d in G.nodes(data=True) if d.get("kind") == "moment"]
    top = sorted(moment_nodes, key=lambda n: -degree.get(n, 0))[:10]
    clusters = _cluster(G)
    sizes = Counter(clusters.values())

    return {
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "moments": len(moment_nodes),
        "terms": G.number_of_nodes() - len(moment_nodes),
        "communities": len(sizes),
        "recurring_bits": [
            {
                "id": n,
                "degree": degree.get(n, 0),
                "start": round(by_id[n].start, 2) if n in by_id else None,
                "text": (by_id[n].text[:140] if n in by_id else ""),
            }
            for n in top if degree.get(n, 0) > 0
        ],
        "arcs": [
            {"community": int(c), "moments": int(sz)}
            for c, sz in sizes.most_common(12)
        ],
        "backend": "graphify" if graphify_available() else "networkx-fallback",
    }