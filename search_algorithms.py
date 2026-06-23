"""
search_algorithms.py
====================
Complete reference implementation of every major search algorithm category.

Categories
----------
1. Array / List Search
   - Linear Search
   - Binary Search
   - Jump Search
   - Interpolation Search
   - Exponential Search

2. Graph Search
   - DFS  (Depth-First Search)
   - BFS  (Breadth-First Search)
   - UCS  (Uniform Cost Search)
   - A*   (A-Star)

3. String Search
   - Naive String Search
   - KMP  (Knuth-Morris-Pratt)
   - Boyer-Moore
   - Rabin-Karp

4. Other
   - Hash Table Lookup  (built-in dict demo + manual chained hash table)

Run:
    python search_algorithms.py
"""

from __future__ import annotations
import math
import heapq
from collections import deque, defaultdict
from typing import Any


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _section(title: str) -> None:
    print(f"\n{'═' * 60}")
    print(f"  {title}")
    print('═' * 60)

def _test(label: str, result: Any, expected: Any) -> None:
    status = "✅ PASS" if result == expected else f"❌ FAIL  (got {result!r})"
    print(f"  {label:<45} {status}")


# ══════════════════════════════════════════════════════════════════════════════
# 1.  ARRAY / LIST SEARCH
# ══════════════════════════════════════════════════════════════════════════════

# ── 1a. Linear Search ────────────────────────────────────────────────────────
# Strategy : check every element left → right until found or end.
# Time     : O(n)   (no pre-conditions needed)

def linear_search(arr: list, target) -> int:
    """Return the first index of *target*, or -1 if not found."""
    for i, val in enumerate(arr):
        if val == target:
            return i
    return -1


# ── 1b. Binary Search ────────────────────────────────────────────────────────
# Strategy : compare target with middle element; discard the wrong half.
# Pre-cond : array MUST be sorted.
# Time     : O(log n)

def binary_search(arr: list, target) -> int:
    """Iterative binary search. Returns index or -1."""
    lo, hi = 0, len(arr) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1


def binary_search_recursive(arr: list, target, lo: int = 0, hi: int = None) -> int:
    """Recursive variant — same logic, same complexity."""
    if hi is None:
        hi = len(arr) - 1
    if lo > hi:
        return -1
    mid = (lo + hi) // 2
    if arr[mid] == target:
        return mid
    if arr[mid] < target:
        return binary_search_recursive(arr, target, mid + 1, hi)
    return binary_search_recursive(arr, target, lo, mid - 1)


# ── 1c. Jump Search ──────────────────────────────────────────────────────────
# Strategy : jump ahead by √n steps to find the block that could contain the
#            target, then do a linear scan inside that block.
# Pre-cond : sorted array.
# Time     : O(√n)

def jump_search(arr: list, target) -> int:
    """Jump search. Returns index or -1."""
    n = len(arr)
    step = int(math.sqrt(n))
    prev = 0

    # Jump forward until we overshoot or reach the end
    while arr[min(step, n) - 1] < target:
        prev = step
        step += int(math.sqrt(n))
        if prev >= n:
            return -1

    # Linear scan inside the current block
    for i in range(prev, min(step, n)):
        if arr[i] == target:
            return i
    return -1


# ── 1d. Interpolation Search ─────────────────────────────────────────────────
# Strategy : probe position estimated by how far the target sits between lo/hi
#            values — like how you open a dictionary near 'Z', not page 1.
# Pre-cond : sorted AND uniformly distributed.
# Time     : O(log log n) average,  O(n) worst

def interpolation_search(arr: list, target) -> int:
    """Interpolation search. Returns index or -1."""
    lo, hi = 0, len(arr) - 1
    while lo <= hi and arr[lo] <= target <= arr[hi]:
        if arr[lo] == arr[hi]:          # All remaining elements equal
            return lo if arr[lo] == target else -1
        # Estimate position
        pos = lo + ((target - arr[lo]) * (hi - lo) // (arr[hi] - arr[lo]))
        if arr[pos] == target:
            return pos
        if arr[pos] < target:
            lo = pos + 1
        else:
            hi = pos - 1
    return -1


# ── 1e. Exponential Search ───────────────────────────────────────────────────
# Strategy : start at index 1, keep doubling until arr[i] >= target or end,
#            then apply binary search in the found range.
# Pre-cond : sorted array.
# Time     : O(log n)   — useful when array is unbounded / very large

def exponential_search(arr: list, target) -> int:
    """Exponential search. Returns index or -1."""
    n = len(arr)
    if n == 0:
        return -1
    if arr[0] == target:
        return 0

    # Find range for binary search
    i = 1
    while i < n and arr[i] <= target:
        i *= 2

    # Binary search within [i//2, min(i, n-1)]
    return binary_search_recursive(arr, target, lo=i // 2, hi=min(i, n - 1))


# ══════════════════════════════════════════════════════════════════════════════
# 2.  GRAPH SEARCH
# ══════════════════════════════════════════════════════════════════════════════
#
# Graph representation used throughout:
#   Unweighted : { node: [neighbour, ...] }
#   Weighted   : { node: [(neighbour, cost), ...] }

# ── 2a. DFS — Depth-First Search ─────────────────────────────────────────────
# Strategy : go as deep as possible along one branch, then backtrack.
# Use-case : cycle detection, topological sort, maze solving.
# Time     : O(V + E)

def dfs(graph: dict, start, goal) -> list | None:
    """
    Return a path from *start* to *goal* using DFS, or None if unreachable.
    Unweighted graph: graph = { node: [neighbours] }
    """
    stack = [(start, [start])]
    visited = set()

    while stack:
        node, path = stack.pop()
        if node == goal:
            return path
        if node in visited:
            continue
        visited.add(node)
        for neighbour in graph.get(node, []):
            if neighbour not in visited:
                stack.append((neighbour, path + [neighbour]))
    return None


# ── 2b. BFS — Breadth-First Search ───────────────────────────────────────────
# Strategy : explore all neighbours at the current depth before going deeper.
# Use-case : shortest path in unweighted graphs, social-network degrees.
# Time     : O(V + E)

def bfs(graph: dict, start, goal) -> list | None:
    """
    Return the shortest (fewest-edge) path from *start* to *goal*, or None.
    """
    queue = deque([(start, [start])])
    visited = {start}

    while queue:
        node, path = queue.popleft()
        if node == goal:
            return path
        for neighbour in graph.get(node, []):
            if neighbour not in visited:
                visited.add(neighbour)
                queue.append((neighbour, path + [neighbour]))
    return None


# ── 2c. UCS — Uniform Cost Search ────────────────────────────────────────────
# Strategy : like BFS but always expand the lowest-cost frontier node.
# Use-case : shortest path in weighted graphs (Dijkstra is a special case).
# Time     : O((V + E) log V)

def ucs(graph: dict, start, goal) -> tuple[list | None, float]:
    """
    Weighted graph: graph = { node: [(neighbour, cost), ...] }
    Returns (path, total_cost) or (None, inf).
    """
    # (cumulative_cost, node, path_so_far)
    heap = [(0, start, [start])]
    visited = {}

    while heap:
        cost, node, path = heapq.heappop(heap)
        if node in visited:
            continue
        visited[node] = cost
        if node == goal:
            return path, cost
        for neighbour, edge_cost in graph.get(node, []):
            if neighbour not in visited:
                heapq.heappush(heap, (cost + edge_cost, neighbour, path + [neighbour]))
    return None, math.inf


# ── 2d. A* Search ────────────────────────────────────────────────────────────
# Strategy : like UCS but adds a heuristic h(n) to guide the search toward
#            the goal. f(n) = g(n) + h(n).
# Use-case : GPS routing, game AI, pathfinding.
# Time     : O((V + E) log V)  — much faster in practice with a good heuristic

def astar(
    graph: dict,
    start,
    goal,
    heuristic: callable = lambda a, b: 0,
) -> tuple[list | None, float]:
    """
    Weighted graph + a heuristic function h(node, goal) → estimated cost.
    Returns (path, total_cost) or (None, inf).

    Default heuristic = 0  (degenerates to UCS / Dijkstra).

    Example heuristic for grid (Manhattan distance):
        h = lambda a, b: abs(a[0]-b[0]) + abs(a[1]-b[1])
    """
    # (f_cost, g_cost, node, path)
    heap = [(heuristic(start, goal), 0, start, [start])]
    best_g = {}

    while heap:
        f, g, node, path = heapq.heappop(heap)
        if node == goal:
            return path, g
        if node in best_g and best_g[node] <= g:
            continue
        best_g[node] = g
        for neighbour, edge_cost in graph.get(node, []):
            new_g = g + edge_cost
            new_f = new_g + heuristic(neighbour, goal)
            heapq.heappush(heap, (new_f, new_g, neighbour, path + [neighbour]))
    return None, math.inf


# ══════════════════════════════════════════════════════════════════════════════
# 3.  STRING SEARCH
# ══════════════════════════════════════════════════════════════════════════════

# ── 3a. Naive String Search ──────────────────────────────────────────────────
# Strategy : slide the pattern over the text, checking every alignment.
# Time     : O(n × m)   n = text length, m = pattern length

def naive_search(text: str, pattern: str) -> list[int]:
    """Return all starting indices where *pattern* appears in *text*."""
    n, m = len(text), len(pattern)
    positions = []
    for i in range(n - m + 1):
        if text[i:i + m] == pattern:
            positions.append(i)
    return positions


# ── 3b. KMP — Knuth-Morris-Pratt ─────────────────────────────────────────────
# Strategy : precompute a 'failure function' (LPS table) that tells how many
#            characters we can skip after a mismatch — never re-compares.
# Time     : O(n + m)

def _kmp_failure_table(pattern: str) -> list[int]:
    """Build the Longest-Proper-Prefix-Suffix (LPS) table."""
    m = len(pattern)
    lps = [0] * m
    length, i = 0, 1
    while i < m:
        if pattern[i] == pattern[length]:
            length += 1
            lps[i] = length
            i += 1
        else:
            if length:
                length = lps[length - 1]
            else:
                lps[i] = 0
                i += 1
    return lps

def kmp_search(text: str, pattern: str) -> list[int]:
    """KMP string search. Returns all starting positions."""
    n, m = len(text), len(pattern)
    if m == 0:
        return []
    lps = _kmp_failure_table(pattern)
    positions, i, j = [], 0, 0
    while i < n:
        if text[i] == pattern[j]:
            i += 1
            j += 1
        if j == m:
            positions.append(i - j)
            j = lps[j - 1]
        elif i < n and text[i] != pattern[j]:
            if j:
                j = lps[j - 1]
            else:
                i += 1
    return positions


# ── 3c. Boyer-Moore (Bad Character heuristic) ────────────────────────────────
# Strategy : compare from the *right* end of the pattern. On mismatch, use the
#            'bad character' table to skip ahead — can skip entire pattern length.
# Time     : O(n/m) best,  O(nm) worst (full version is O(n+m))

def _bad_char_table(pattern: str) -> dict[str, int]:
    """Map each character in pattern to its last occurrence index."""
    return {ch: i for i, ch in enumerate(pattern)}

def boyer_moore_search(text: str, pattern: str) -> list[int]:
    """Boyer-Moore (bad character rule). Returns all starting positions."""
    n, m = len(text), len(pattern)
    if m == 0 or m > n:
        return []
    bad_char = _bad_char_table(pattern)
    positions = []
    s = 0                   # shift of pattern relative to text
    while s <= n - m:
        j = m - 1
        while j >= 0 and pattern[j] == text[s + j]:
            j -= 1
        if j < 0:
            positions.append(s)
            # Shift so the next character in text aligns with the last
            # occurrence of that character in pattern (or by 1 if past end)
            s += m - bad_char.get(text[s + m], -1) if s + m < n else 1
        else:
            shift = j - bad_char.get(text[s + j], -1)
            s += max(1, shift)
    return positions


# ── 3d. Rabin-Karp ───────────────────────────────────────────────────────────
# Strategy : use a rolling hash so the comparison of each window against the
#            pattern hash is O(1); recompute by rolling the window.
# Time     : O(n + m) average,  O(nm) worst (hash collisions)

def rabin_karp_search(text: str, pattern: str,
                      base: int = 256, mod: int = 101) -> list[int]:
    """Rabin-Karp. Returns all starting positions."""
    n, m = len(text), len(pattern)
    if m == 0 or m > n:
        return []

    h = pow(base, m - 1, mod)          # base^(m-1) mod prime
    p_hash = 0                          # pattern hash
    t_hash = 0                          # current window hash
    positions = []

    for i in range(m):
        p_hash = (base * p_hash + ord(pattern[i])) % mod
        t_hash = (base * t_hash + ord(text[i])) % mod

    for i in range(n - m + 1):
        if p_hash == t_hash:
            if text[i:i + m] == pattern:   # Verify (handles collisions)
                positions.append(i)
        if i < n - m:
            t_hash = (base * (t_hash - ord(text[i]) * h) + ord(text[i + m])) % mod
            if t_hash < 0:
                t_hash += mod
    return positions


# ══════════════════════════════════════════════════════════════════════════════
# 4.  HASH TABLE LOOKUP
# ══════════════════════════════════════════════════════════════════════════════
# Strategy : apply a hash function to map a key → bucket index → O(1) average.

class HashTable:
    """
    Simple chained hash table (separate chaining for collisions).
    Demonstrates the internals behind Python's dict.
    Time: O(1) average insert/search, O(n) worst (all keys collide).
    """

    def __init__(self, capacity: int = 16):
        self._cap = capacity
        self._buckets: list[list] = [[] for _ in range(capacity)]
        self._size = 0

    def _bucket(self, key) -> int:
        return hash(key) % self._cap

    def put(self, key, value) -> None:
        b = self._bucket(key)
        for pair in self._buckets[b]:
            if pair[0] == key:
                pair[1] = value
                return
        self._buckets[b].append([key, value])
        self._size += 1

    def get(self, key, default=None):
        b = self._bucket(key)
        for pair in self._buckets[b]:
            if pair[0] == key:
                return pair[1]
        return default

    def __contains__(self, key) -> bool:
        return self.get(key, None) is not None

    def __len__(self) -> int:
        return self._size


# ══════════════════════════════════════════════════════════════════════════════
# TEST SUITE
# ══════════════════════════════════════════════════════════════════════════════

def run_tests() -> None:

    # ── Array / List Search ──────────────────────────────────────────────────
    _section("1. ARRAY / LIST SEARCH")

    arr_unsorted = [4, 2, 7, 1, 9, 3, 5, 8, 6, 0]
    arr_sorted   = sorted(arr_unsorted)           # [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    arr_uniform  = list(range(0, 100, 10))         # [0, 10, 20, ..., 90]

    print(f"\n  sorted array  : {arr_sorted}")
    print(f"  uniform array : {arr_uniform}\n")

    # Linear (works on unsorted)
    _test("linear_search([4,2,7,1,9,3,5,8,6,0], 7)",  linear_search(arr_unsorted, 7),  2)
    _test("linear_search([4,2,7,1,9,3,5,8,6,0], 99)", linear_search(arr_unsorted, 99), -1)

    # Binary
    _test("binary_search(sorted, 5)",                  binary_search(arr_sorted, 5),    5)
    _test("binary_search(sorted, 0)",                  binary_search(arr_sorted, 0),    0)
    _test("binary_search(sorted, 99)",                 binary_search(arr_sorted, 99),  -1)
    _test("binary_search_recursive(sorted, 8)",        binary_search_recursive(arr_sorted, 8), 8)

    # Jump
    _test("jump_search(sorted, 6)",                    jump_search(arr_sorted, 6),      6)
    _test("jump_search(sorted, 99)",                   jump_search(arr_sorted, 99),    -1)

    # Interpolation
    _test("interpolation_search(uniform, 50)",         interpolation_search(arr_uniform, 50), 5)
    _test("interpolation_search(uniform, 99)",         interpolation_search(arr_uniform, 99), -1)

    # Exponential
    _test("exponential_search(sorted, 9)",             exponential_search(arr_sorted, 9), 9)
    _test("exponential_search(sorted, 99)",            exponential_search(arr_sorted, 99), -1)

    # ── Graph Search ─────────────────────────────────────────────────────────
    _section("2. GRAPH SEARCH")

    # Unweighted graph (for DFS / BFS)
    ug = {
        "A": ["B", "C"],
        "B": ["A", "D", "E"],
        "C": ["A", "F"],
        "D": ["B"],
        "E": ["B", "F"],
        "F": ["C", "E"],
    }

    # Weighted graph (for UCS / A*)
    # Format: { node: [(neighbour, cost), ...] }
    wg = {
        "A": [("B", 1), ("C", 4)],
        "B": [("A", 1), ("D", 2), ("E", 5)],
        "C": [("A", 4), ("F", 3)],
        "D": [("B", 2)],
        "E": [("B", 5), ("F", 1)],
        "F": [("C", 3), ("E", 1)],
    }

    print(f"\n  Unweighted: A─B─D, A─B─E─F, A─C─F")
    print(f"  Weighted  : edges with varying costs\n")

    dfs_path = dfs(ug, "A", "F")
    _test("dfs  : A → F  (any path)",    dfs_path is not None and dfs_path[0] == "A" and dfs_path[-1] == "F",  True)

    bfs_path = bfs(ug, "A", "F")
    _test("bfs  : A → F  shortest hops", bfs_path, ["A", "C", "F"])

    ucs_path, ucs_cost = ucs(wg, "A", "F")
    _test("ucs  : A → F  lowest cost",   ucs_path, ["A", "B", "E", "F"])
    _test("ucs  : A → F  cost = 7",      ucs_cost, 7)

    # A* with zero heuristic (= UCS)
    astar_path, astar_cost = astar(wg, "A", "F")
    _test("a*   : A → F  path (h=0)",    astar_path, ["A", "B", "E", "F"])
    _test("a*   : A → F  cost = 7",      astar_cost, 7)

    print(f"\n  DFS path (A→F)  : {dfs_path}")
    print(f"  BFS path (A→F)  : {bfs_path}")
    print(f"  UCS path (A→F)  : {ucs_path}  (cost = {ucs_cost})")
    print(f"  A*  path (A→F)  : {astar_path}  (cost = {astar_cost})")

    # ── String Search ─────────────────────────────────────────────────────────
    _section("3. STRING SEARCH")

    text    = "the cat sat on the catfish near the cat"
    pattern = "cat"
    expected_positions = [4, 19, 36]   # "the cat(4) ... catfish(19) ... the cat(36)"

    print(f"\n  text    : \"{text}\"")
    print(f"  pattern : \"{pattern}\"")
    print(f"  expected positions : {expected_positions}\n")

    _test("naive_search       positions", naive_search(text, pattern),        expected_positions)
    _test("kmp_search         positions", kmp_search(text, pattern),           expected_positions)
    _test("boyer_moore_search positions", boyer_moore_search(text, pattern),   expected_positions)
    _test("rabin_karp_search  positions", rabin_karp_search(text, pattern),    expected_positions)

    # Edge cases
    _test("kmp: pattern not found",  kmp_search("hello world", "xyz"),  [])
    _test("kmp: empty pattern",      kmp_search("hello", ""),           [])
    _test("naive: overlapping 'aa' in 'aaaa'", naive_search("aaaa", "aa"), [0, 1, 2])

    # ── Hash Table ────────────────────────────────────────────────────────────
    _section("4. HASH TABLE LOOKUP")

    ht = HashTable(capacity=8)
    ht.put("name",    "Alice")
    ht.put("age",     25)
    ht.put("city",    "Vadodara")
    ht.put("project", "Crowd Counter")

    print(f"\n  Inserted 4 keys into HashTable(capacity=8)\n")

    _test("get('name')     == 'Alice'",     ht.get("name"),    "Alice")
    _test("get('age')      == 25",          ht.get("age"),     25)
    _test("get('city')     == 'Vadodara'",  ht.get("city"),    "Vadodara")
    _test("get('missing')  == None",        ht.get("missing"), None)
    _test("'project' in ht == True",        "project" in ht,   True)
    _test("'ghost'   in ht == False",       "ghost"   in ht,   False)
    _test("len(ht)         == 4",           len(ht),           4)

    # Update existing key
    ht.put("age", 26)
    _test("update age → 26",               ht.get("age"),     26)
    _test("len stays 4 after update",       len(ht),           4)

    print()


# ══════════════════════════════════════════════════════════════════════════════
# QUICK CHEAT-SHEET  (printed on import / run)
# ══════════════════════════════════════════════════════════════════════════════

CHEATSHEET = """
╔══════════════════════════════════════════════════════════════╗
║              SEARCH ALGORITHM CHEAT-SHEET                   ║
╠══════════════════════════════════════════════════════════════╣
║  ARRAY / LIST                                               ║
║  ─────────────────────────────────────────────────────────  ║
║  Linear        O(n)          any array, one by one         ║
║  Binary        O(log n)      sorted,  cut in half          ║
║  Jump          O(√n)         sorted,  hop fixed blocks     ║
║  Interpolation O(log log n)  sorted + uniform, smart guess ║
║  Exponential   O(log n)      sorted / unbounded array      ║
║                                                             ║
║  GRAPH                                                      ║
║  ─────────────────────────────────────────────────────────  ║
║  DFS           O(V+E)        go deep, then backtrack       ║
║  BFS           O(V+E)        go wide, shortest #hops       ║
║  UCS           O((V+E)logV)  cheapest-cost path first      ║
║  A*            O((V+E)logV)  UCS + heuristic guide         ║
║                                                             ║
║  STRING                                                     ║
║  ─────────────────────────────────────────────────────────  ║
║  Naive         O(nm)         every alignment checked       ║
║  KMP           O(n+m)        LPS table skips re-compares   ║
║  Boyer-Moore   O(n/m) best   compare from right, skip far  ║
║  Rabin-Karp    O(n+m) avg    rolling hash windows          ║
║                                                             ║
║  OTHER                                                      ║
║  ─────────────────────────────────────────────────────────  ║
║  Hash Table    O(1) avg      hash fn → bucket → O(1)       ║
╚══════════════════════════════════════════════════════════════╝
"""


if __name__ == "__main__":
    print(CHEATSHEET)
    run_tests()
    print("\n  All tests complete.\n")
