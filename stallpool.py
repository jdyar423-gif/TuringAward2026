"""
stallpool.py -- least-stuck pool: cross-game allocation of model turns for an ARC-AGI-3 harness that
plays many games on few GPU streams inside one shared solving window.

THE ONE DECISION IT CHANGES
  At each model-turn boundary: which game's next ORDINARY turn runs. A game that has spent >= K turns
  on its current level without clearing it hands its stream to a waiting game that is less stuck; it
  is resumed later with its conversation and board untouched. Nothing inside any game changes: no
  RESET, no context rollback, no extra prompt text, no extra model call.

STATUS
  * replay() / replay_report(): EXECUTED on public Duck-harness traces of Qwen3.8-Flash-Next
    (adsdemaybe/arc-agi-3 runs kaggle_submission-v1, -v2). Exact arc_agi 0.9.9 scoring of prefixes.
  * StallPool (sync core) is the SAME object replay() uses for the treatment arm.
  * AsyncStallPool: EXECUTED only in _selftest() with scripted games.
  * UNTESTED inside V4, on arcengine, on Kaggle. No score gain on V4 has been measured.

CONTRACT
  offline (CPU only): traces = traces_from_records(recs); print(replay_report(traces, slots=S))
    recs: one dict per executed action, in order, per game:
      game (str), n_levels (int), base (list[int] human baseline per level; offline scoring only),
      levels_completed (int, after the action), new_turn (bool, first action of a model reply),
      t (float, wall-clock seconds at the end of the action, game-local clock starting at 0)
  online: pool = AsyncStallPool(StallPool(slots=S, K=K)); pool.register(gid, n_levels) for ALL games
          per game:  while await pool.acquire(gid):          # False once the game is done
                         <one unchanged V4 model reply + its env.step calls>
                         await pool.turn_end(gid, fd.levels_completed, done=(fd.state == WIN))
"""
from __future__ import annotations

import asyncio
import heapq
import json
import random
import statistics
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


def level_score(baseline: float, actions: float) -> float:          # arc_agi 0.9.9 scorecard
    return min(115.0, 100.0 * (baseline / max(actions, 1.0)) ** 2)


def level_weight(level: int, n_levels: int) -> float:               # weight k / sum_k k
    return level / (n_levels * (n_levels + 1) / 2.0)


# ------------------------------------------------------------------------------------------------
# the gate (sync core; identical logic online and in replay)
# ------------------------------------------------------------------------------------------------

class StallPool:
    def __init__(self, slots: int, K: int):
        self.S, self.K = slots, K
        self.st: Dict[str, Dict[str, Any]] = {}
        self.order: Dict[str, int] = {}
        self.waiting: List[str] = []
        self.active: set = set()
        self.log: List[str] = []

    def register(self, gid: str, n_levels: int) -> None:
        self.st[gid] = dict(lv=0, ton=0, n=n_levels, done=False)
        self.order[gid] = len(self.order)
        self.waiting.append(gid)

    def _pick(self) -> Optional[str]:
        c = [g for g in self.waiting if not self.st[g]["done"]]
        if not c:
            return None
        return min(c, key=lambda g: (self.st[g]["ton"], -self.st[g]["lv"], self.order[g]))

    def fill(self) -> List[str]:
        new = []
        while len(self.active) < self.S:
            g = self._pick()
            if g is None:
                break
            self.waiting.remove(g)
            self.active.add(g)
            new.append(g)
            self.log.append(f"admit {g} ton={self.st[g]['ton']} lv={self.st[g]['lv']}")
        return new

    def on_turn_end(self, gid: str, levels_completed: int, done: bool = False) -> bool:
        """Call once per model reply of gid, after its last env.step. Returns True if gid keeps its slot."""
        s = self.st[gid]
        if levels_completed > s["lv"]:
            s["lv"], s["ton"] = levels_completed, 0
        else:
            s["ton"] += 1
        s["done"] = done or s["lv"] >= s["n"]
        keep = not s["done"]
        if keep and s["ton"] >= self.K:
            y = self._pick()
            if y is not None and self.st[y]["ton"] < s["ton"]:
                keep = False
                self.log.append(f"park {gid} ton={s['ton']} lv={s['lv']} for {y} ton={self.st[y]['ton']}")
        if not keep:
            self.active.discard(gid)
            if not s["done"]:
                self.waiting.append(gid)
            self.fill()
        return keep


class AsyncStallPool:
    """asyncio wrapper. A game holding a slot passes acquire() immediately; a parked game waits."""

    def __init__(self, pool: StallPool):
        self.p = pool
        self.cv: Optional[asyncio.Condition] = None

    def register(self, gid: str, n_levels: int) -> None:
        self.p.register(gid, n_levels)

    async def acquire(self, gid: str) -> bool:
        if self.cv is None:
            self.cv = asyncio.Condition()
        async with self.cv:
            self.p.fill()
            await self.cv.wait_for(lambda: gid in self.p.active or self.p.st[gid]["done"])
        return not self.p.st[gid]["done"]

    async def turn_end(self, gid: str, levels_completed: int, done: bool = False) -> None:
        async with self.cv:
            self.p.on_turn_end(gid, levels_completed, done)
            self.cv.notify_all()


# ------------------------------------------------------------------------------------------------
# exact replay on real traces (same stream-seconds; each game can only replay turns it really played)
# ------------------------------------------------------------------------------------------------

@dataclass
class Trace:
    game: str
    n_levels: int
    sec: List[float]                    # duration of each logged model turn
    ps: List[tuple] = field(default_factory=list)   # after m turns: (levels_done, turns_on_level, game_score)

    @property
    def window(self) -> float:
        return sum(self.sec)


def traces_from_records(recs: Sequence[Dict[str, Any]]) -> List[Trace]:
    by: Dict[str, List[Dict[str, Any]]] = {}
    for r in recs:
        by.setdefault(str(r["game"]), []).append(r)
    out = []
    for game, rows in by.items():
        n, base = int(rows[0]["n_levels"]), rows[0]["base"]
        turns: List[List[Dict[str, Any]]] = []
        for r in rows:
            if r["new_turn"] or not turns:
                turns.append([])
            turns[-1].append(r)
        sec, ps, prev_t = [], [(0, 0, 0.0)], 0.0
        lvl, acts, sc, ton = 0, 0, 0.0, 0
        for tr in turns:
            ton += 1
            for r in tr:
                acts += 1
                if int(r["levels_completed"]) > lvl:
                    sc += level_weight(lvl + 1, n) * level_score(base[lvl], acts)
                    lvl, acts, ton = int(r["levels_completed"]), 0, 0
            sec.append(max(float(tr[-1]["t"]) - prev_t, 1.0))
            prev_t = float(tr[-1]["t"])
            ps.append((lvl, ton, sc))
        out.append(Trace(game, n, sec, ps))
    return out


def replay(traces: Sequence[Trace], slots: int, frac: float, policy: str, K: int = 8,
           order: Optional[Sequence[int]] = None) -> Dict[str, float]:
    """policy 'cap'  : FIFO admission, each game keeps its slot until it has used frac * its own logged
                       window (the per-game budget a fixed-window / wave scheduler gives), or is done.
       policy 'pool' : StallPool(slots, K).
       Both arms get the same stream-seconds: horizon H = sum_g frac * window_g / slots."""
    idx = list(order) if order is not None else list(range(len(traces)))
    T = [traces[i] for i in idx]
    H = sum(frac * t.window for t in T) / slots
    m = [0] * len(T)
    used = [0.0] * len(T)

    def alive(i):
        return m[i] < len(T[i].sec) and T[i].ps[m[i]][0] < T[i].n_levels
    pool = StallPool(slots, K)
    for i, t in enumerate(T):
        pool.register(str(i), t.n_levels)
    clock: List[tuple] = []
    if policy == "pool":
        start = [int(g) for g in pool.fill()]
    else:
        start = list(range(min(slots, len(T))))
        nxt = len(start)
    for i in start:
        heapq.heappush(clock, (T[i].sec[0], i))
    while clock:
        now, i = heapq.heappop(clock)
        if now > H:
            break
        used[i] += T[i].sec[m[i]]
        m[i] += 1
        if policy == "pool":
            before = set(pool.active)
            keep = pool.on_turn_end(str(i), T[i].ps[m[i]][0], done=not alive(i))
            if keep:
                heapq.heappush(clock, (now + T[i].sec[m[i]], i))
            for g in pool.active - before:
                j = int(g)
                heapq.heappush(clock, (now + T[j].sec[m[j]], j))
        else:
            if alive(i) and used[i] < frac * T[i].window:
                heapq.heappush(clock, (now + T[i].sec[m[i]], i))
            else:
                while nxt < len(T) and not alive(nxt):
                    nxt += 1
                if nxt < len(T):
                    heapq.heappush(clock, (now + T[nxt].sec[0], nxt))
                    nxt += 1
    score = sum(T[i].ps[m[i]][2] for i in range(len(T))) / len(T)
    levels = sum(T[i].ps[m[i]][0] for i in range(len(T)))
    return dict(score=score, levels=levels)


def replay_report(traces: Sequence[Trace], slots: int, fracs=(0.5, 0.75), Ks=(4, 8, 12, 16),
                  n_orders: int = 40, seed: int = 0) -> str:
    rng = random.Random(seed)
    orders = []
    for _ in range(n_orders):
        o = list(range(len(traces)))
        rng.shuffle(o)
        orders.append(o)
    lines = []
    for f in fracs:
        base = [replay(traces, slots, f, "cap", order=o) for o in orders]
        b = statistics.mean(x["score"] for x in base)
        cells = []
        for K in Ks:
            d = sorted(replay(traces, slots, f, "pool", K, o)["score"] - x["score"] for o, x in zip(orders, base))
            cells.append(f"K={K}: {statistics.mean(d):+.2f} [p10 {d[len(d) // 10]:+.2f}]")
        lines.append(f"slots={slots} budget={f:.0%}  cap-baseline RHAE {b:.2f} | pool - cap: " + " | ".join(cells))
    return "\n".join(lines)


def records_from_duck(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Adapter for public Duck benchmark.json files."""
    out = []
    for g in obj["game_runs"]:
        bounds, c = [], 0
        for n in g["actions_per_level"]:
            c += n
            bounds.append(c)
        for i, h in enumerate(g["history"]):
            lc = sum(1 for b in bounds[: g["levels_completed"]] if i + 1 >= b)
            out.append(dict(game=g["game_id"][:4], n_levels=g["number_of_levels"], base=g["base_actions_per_level"],
                            levels_completed=lc, new_turn=h["generated_tokens"] > 0, t=h["wallclock_seconds"]))
    return out


# ------------------------------------------------------------------------------------------------

def _selftest() -> None:
    # core: 1 slot, K=2. 'a' is stuck; 'b' waits with 0 turns -> 'a' is parked at its 2nd stuck turn.
    p = StallPool(slots=1, K=2)
    p.register("a", 3)
    p.register("b", 3)
    assert p.fill() == ["a"]
    assert p.on_turn_end("a", 0) is True                      # ton=1 < K
    assert p.on_turn_end("a", 0) is False and p.active == {"b"}   # ton=2 >= K, b less stuck
    assert p.on_turn_end("b", 1) is True                      # cleared a level: ton resets, keeps slot
    for _ in range(2):
        p.on_turn_end("b", 1)                                 # b stuck 2 turns; a (ton=2) is not less stuck
    assert p.active == {"b"}
    p.on_turn_end("b", 1)                                     # b ton=3 > a ton=2 -> swap back to a
    assert p.active == {"a"}, p.log

    # replay on a toy: game 0 clears a level every turn, game 1 never does; 1 slot, budget 50%.
    tr = [Trace("x", 4, [1.0] * 4, [(0, 0, 0.0), (1, 0, 10.0), (2, 0, 20.0), (3, 0, 30.0), (4, 0, 40.0)]),
          Trace("y", 4, [1.0] * 4, [(0, 0, 0.0), (0, 1, 0.0), (0, 2, 0.0), (0, 3, 0.0), (0, 4, 0.0)])]
    cap = replay(tr, 1, 0.5, "cap", order=[1, 0])
    pool = replay(tr, 1, 0.5, "pool", K=1, order=[1, 0])
    assert cap["score"] == 10.0 and pool["score"] == 15.0, (cap, pool)   # cap: y burns 2 s, x gets 2 s

    async def loop():                                          # async wrapper: 4 games, 2 slots, no deadlock
        ap = AsyncStallPool(StallPool(slots=2, K=2))
        for g in "wxyz":
            ap.register(g, 2)
        seen = []

        async def play(g):
            lc = 0
            while await ap.acquire(g):
                seen.append(g)
                await asyncio.sleep(0)
                lc += 1 if (g in "wx" or len(seen) > 6) else 0
                await ap.turn_end(g, lc)
        await asyncio.wait_for(asyncio.gather(*(play(g) for g in "wxyz")), 5)
        return seen, ap
    seen, ap = asyncio.run(loop())
    assert set(seen) == set("wxyz") and all(s["done"] for s in ap.p.st.values()), seen
    print("selftest OK")


if __name__ == "__main__":
    if sys.argv[1:2] == ["replay"] and len(sys.argv) > 2:
        recs = [json.loads(l) for l in open(sys.argv[2]) if l.strip()]
        print(replay_report(traces_from_records(recs), slots=int(sys.argv[3]) if len(sys.argv) > 3 else 6))
    else:
        _selftest()
