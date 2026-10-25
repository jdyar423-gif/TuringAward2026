# ARC-AGI-3 交接 v3：撤回停滯重開，改押 least-stuck pool（跨遊戲 turn 分配）

標記：**[已執行]** 指本 session 跑過，數字由下方 kernel 原樣產生。**[未測試]** 指未在 V4、arcengine 或 Kaggle 上跑過。**[假設]** 指沒有 V4 數據。

---

## 0. 決定

1. **停滯重開不是我最值得押注的機制。** HCR（RESET＋context rollback）和 stale_restart（保留盤面、只回退 context）都撤回，不再修補。你們三個反例都成立。
2. **上一版 SRTA 也撤回：** frailty index、合成模擬、「+5～+8」都不再作為依據。
3. **改押 least-stuck pool。**
   - 只改一個決策：每個 model-turn 邊界，**哪個遊戲的下一個「普通」turn 先跑**。
   - 一個遊戲在當前關卡已連續花了 ≥K 個 turns 未通關，且有比它更不卡的遊戲在等，就讓出 stream。它的對話和盤面都不動，之後再回來。
   - 不 RESET、不回退 context、不加 prompt、不加 model call。
4. **決策依據是精確的同預算 RHAE，不是合成勝率。** 在兩份同模型公開真實 trace 上，用 scorecard 逐關精確計分、同樣的 stream 秒數 [已執行]：
   - Duck v1：+3.6～+4.1 RHAE（baseline 5.9／8.8）；
   - Duck v2：+0.25～+0.74 RHAE（baseline 8.2／9.4）；
   - 對 40 種遊戲順序取平均，沒有負值；最差的 p10 是 −0.27。
   - 這是真實但小樣本、跨 run 不穩定的證據。V4 上的效果未知。
5. **進入下一輪的路徑：**
   1. 零 GPU：在 V4 自己的 log 上跑同一個 replay，選 K 並決定去留；
   2. 一次分池同步的本地配對 run（兩個 arm 同時跑在同一個 server 上，約 7.8 GPU-h）；
   3. 一次 Kaggle 提交。

---

## 1. 為何停滯重開不能押

我接受你們的論點，再補一點成本面：

- **識別。** 同一條 population survival，可能來自固定難度，也可能來自可被重開消除的隨機錨定。未干預 log 和跨 run discordance 分不出兩者。任何「何時重開」的規則都必須預設一個無法驗證的分布：你們的雙峰反例裡 continue 成功率是 1，而 iid 重抽兩次只有 0.84。我的擬合在停止時刻上也有偏（κ=1.31 對真值 1）。
- **成本。** 重開讓該關的累計 actions 增加，效率項按平方扣分；level-up 增加不等於 RHAE 增加。
- **外部證據不支持。**
  - Fail-Fast warm restart 的 +5.2，伴隨淨生成 tokens +43.8%；cold restart 只有 +0.2。
  - rellik13 的 fresh-context restart 是 null。
  - adsdemaybe v1／v2 的配置不同，不能當作同一策略的 iid 重跑。
- **結論。** 要估重開的效果只能做隨機化介入，而目前的先驗不值得把 GPU 花在這裡。

---

## 2. 新機制：它改變哪個決策；為什麼它的反事實能從未干預 log 精確算出

**規則**（`StallPool`，線上與 replay 用的是同一個 class）：

- `ton` = 當前關卡自上次 level-up 以來的 model turns。level-up 時歸零；RESET 或 GAME_OVER 不歸零，因為關卡沒變。
- 遊戲 x 每個 turn 結束時：
  - x 已完成全部關卡或 WIN → 釋放 slot；
  - `ton(x) ≥ K`，且等待中有遊戲 y 滿足 `ton(y) < ton(x)` → x 回到等待佇列，盤面與對話原封不動；
  - 空出的 slot 交給等待中最不卡的遊戲：先比 `ton` 小，再比已完成關卡多，最後依登記順序。
- 所有遊戲在開始時登記到同一個 pool。未開始的遊戲 `ton=0`，所以每個遊戲都會很早被 admit，不會被跳過。

**為什麼反事實可識別：** 這個 gate **不對任何遊戲做新的介入**。每個遊戲執行的永遠是它自己的下一個普通 turn；gate 只決定誰的 turn 先跑、何時跑。在 A1（§4）下：

- 任何「給遊戲 g 它前 m_g 個 turns」的分配，其分數就是該前綴的精確 scorecard 分數：每關的 actions 是實際的，平方扣分也是精確的。
- 所以兩種分配在同樣 stream 秒數下的比較，是對真實 log 的精確計算。不需要存活曲線，不需要重開效果，不需要合成世界。
- 這正是重開缺少的性質。

**為什麼幫陌生遊戲：**

- 一個在同一關卡了很多 turns 的遊戲，之後每個 turn 幾乎不產分：通關率低，就算通關，效率也已崩。
- 剛清一關的遊戲處在權重更高、效率重新開始的新關。
- 陌生遊戲落在長時間卡住狀態的比例較高（other15 29.82 對 dev10 76.27）。gate 把這段時間移給正在前進的遊戲。
- 卡住的遊戲沒有被放棄：沒有更不卡的遊戲在等時，它就回來。
- 它不讀畫面內容，所以不會把畫面不變判成無效動作，也不會把唯一移動塊判成主角。

---

## 3. 可驗算的決策依據 [已執行]

方法：

- baseline = 每遊戲固定窗、FIFO 接續：每個遊戲用完自己 log 窗的 `budget` 比例，或打完全部關卡，就交出 slot。這是 wave 或固定 cap 排程的形式，也是 lordhansolo 的形式。
- 兩個 arm 的總 stream 秒數相同；turn 時長取 log 實值。
- 數字是 40 種隨機遊戲順序的平均，方括號是 p10。

| run | slots | budget | baseline RHAE | K=4 | K=8 | K=12 | K=16 |
|---|---|---|---|---|---|---|---|
| Duck v1 | 6 | 50% | 5.89 | +4.08 [+3.65] | +3.91 [+3.43] | +3.50 [+2.12] | +2.15 [−0.01] |
| Duck v1 | 6 | 75% | 8.83 | +3.70 [+3.25] | +3.65 [+3.23] | +3.62 [+3.18] | +3.54 [+2.58] |
| Duck v1 | 8 | 50% | 5.85 | +3.78 [+3.42] | +3.62 [+2.27] | +3.09 [+1.47] | +2.08 [+0.37] |
| Duck v1 | 8 | 75% | 8.84 | +3.68 [+3.40] | +3.68 [+3.40] | +3.57 [+3.40] | +3.37 [+2.26] |
| Duck v2 | 6 | 50% | 8.18 | +0.32 [−0.09] | +0.31 [−0.09] | −0.10 [−0.32] | −1.14 [−3.39] |
| Duck v2 | 6 | 75% | 9.37 | +0.74 [+0.34] | +0.69 [+0.17] | +0.71 [+0.22] | +0.72 [+0.21] |
| Duck v2 | 8 | 50% | 8.18 | +0.25 [−0.25] | +0.25 [−0.27] | −0.13 [−0.58] | −0.88 [−2.70] |
| Duck v2 | 8 | 75% | 9.36 | +0.48 [−0.07] | +0.52 [+0.02] | +0.68 [+0.13] | +0.83 [+0.24] |

讀法：

- **K 小（4–8）**：兩個 run 的平均都不為負。v1 大（相對 +41～+69%），v2 小（+3～+8%）。
- **K 大（16）**：在 v2 的 50% budget 會輸（−0.9～−1.1）。
- **為何 v2 小：** v2 中被 park 的遊戲事後仍清了較多關卡。以「≥16 turns 後永不恢復」做精確界（另以記憶體內腳本算出 [已執行]）：這些遊戲事後的得分 v1 只有 0.31，v2 有 1.26 RHAE。
- **resume 的 re-prefill 成本：** 每 run 46–233 次 park，每次以最壞 6 s 計，≤ 0.7% 的 stream 秒數。

**replay 不能證明的部分：**

1. 遊戲不能重播它沒打過的 turn。所以在 hidden 上「讓前進中的遊戲超過舊固定窗繼續打」的收益，**完全沒有被計入**；這部分 replay 是保守的。
2. budget 100% 時，replay 依構造約為 0。
3. 全預算下的真實效果只能由 live run 量（§7）。

**會區分它與 baseline 的具體軌跡**（真實 trace：Duck v1，t=3,168 s，即窗的 40% 處）：

| 狀態 | 遊戲（關卡／本關已花 turns／本關已花 actions） | t 之後真實發生的 |
|---|---|---|
| 最卡的 1/4 | sc25 1/14/60、tn36 1/17/63、bp35 1/12/49、wa30 1/17/53、sk48 1/17/62、dc22 1/13/101 | 又用了 129 turns，每 turn 0.017 分 |
| 最前進的 1/4 | ft09 4/5/37、r11l 3/1/7、lp85 3/4/6、ar25 3/1/5、tr87 2/0/0 | 只用了 78 turns，每 turn 1.333 分 |

- **baseline**：最卡的 6 個遊戲各自占著 stream 直到自己的窗結束。
- **K=8 的 gate**：它們的 `ton`（12–17）≥ 8，而等待佇列裡有 ton 更小的遊戲，所以在各自下一個 turn 邊界讓出 stream。前進中的遊戲（ton 0–5）不受影響。

---

## 4. 剩下的分布／因果假設（明列）

- **A1 排程不變性。** 遊戲 g 第 k 個 turn 的結果分布，只取決於它自己前 k−1 個 turns；與何時跑、暫停多久、其他遊戲無關。
  - 會破壞 A1 的情況：V4 prompt 含牆鐘或剩餘時間文字；V4 的 reply 長度以時間而不是 token 截斷。
  - KV re-prefill 不改變內容。
  - 分池同步的設計中，兩個 arm 的 server 負載相同。
- **A2 截斷。** replay 只用 log 裡存在的 turns，所以 hidden 上的額外上行未被量到，只能 live 量。
- **A3 K 的轉移。** K 是 turn 計數；V4 每遊戲的 turns 約是 Duck 的 3 倍，所以 K 必須在 V4 自己的 log 上選（§7 Step 0），然後原樣用在 hidden。
- **A4 結構。** hidden run 的遊戲數多於 slots，且 V4 目前給每遊戲固定窗（分批或固定 cap）。若 V4 是 110 遊戲全常駐、等額分享，gate 必須把 S 設得小於 G，這會改變每 turn 延遲。此時 replay 的時長不適用，只能看 live 結果。

---

## 5. Kernel（`stallpool.py`；`python3 stallpool.py` 跑 selftest，`python3 stallpool.py replay recs.jsonl S` 跑 replay）

```python
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
```

執行紀錄 [已執行]：

- `selftest OK`。core 的 park／回補規則、toy replay（cap 10.0 對 pool 15.0）、asyncio 4 遊戲 2 slots 無 deadlock 都通過。
- §3 的表由 `replay_report(traces_from_records(records_from_duck(obj)), slots=S)` 原樣產生，兩個 run 共約 3 s CPU。
- 本 repo 內重現：`python3 stallpool.py`（selftest）、`python3 replay_public_duck.py`（§3 整張表）。

---

## 6. 插點、input、output

| 位置 | 呼叫 | Input | Output |
|---|---|---|---|
| run 開始（取代分批或每遊戲固定窗） | 對全部遊戲呼叫 `pool.register(gid, n_levels)` | `FrameData.win_levels`；拿不到就用 7，此值只用來判斷是否打完 | — |
| 每個 model request 之前 | `await pool.acquire(gid)` | — | True 表示可以發這一個 reply；False 表示已打完。被 park 的遊戲在這裡等待。 |
| 該 reply 的最後一個 `env.step` 回傳後（native step callback 讀到 FrameData 的位置） | `await pool.turn_end(gid, fd.levels_completed, done=(fd.state == WIN))` | 本 turn 結束後的 `levels_completed` | 留下或讓出 slot；`pool.p.log` 記錄每次 admit／park 與當時的 ton、lv |

V4 的 prompt、context、batch／cut、RESET／GAME_OVER 處理、逐 tick callback 全部不變。被 park 的遊戲只是不發下一個 request。

```python
# V4 integration sketch  [未測試: V4 source not seen; v4_* are V4's own functions]
import asyncio, time
from stallpool import StallPool, AsyncStallPool

async def run_all(game_ids, solve_seconds, S, K):
    pool = AsyncStallPool(StallPool(slots=S, K=K))
    for gid in game_ids:
        fd = v4_make_and_reset(gid)                     # unchanged competition-mode setup
        pool.register(gid, fd.win_levels)
    tasks = [asyncio.create_task(play(gid, pool)) for gid in game_ids]
    await asyncio.wait(tasks, timeout=solve_seconds)    # shared deadline, unchanged
    for t in tasks:
        t.cancel()

async def play(gid, pool):
    while await pool.acquire(gid):
        fd = await v4_one_model_turn(gid)               # unchanged: one reply + its env.step calls
        await pool.turn_end(gid, fd.levels_completed, done=(fd.state.name == "WIN"))
```

---

## 7. 最省 GPU 的同預算隨機對照與淘汰標準

**Step 0（零 GPU，V4 log；可併入你們正在做的 reply 抽取）**

- 需要的欄位，每個 action 一行：game、win_levels、執行後的 levels_completed、new_turn（reply 的第一個 action）、遊戲內牆鐘 t、各關 baseline（只用於離線計分）。
- 對每個 V4 public25 run **分別**跑 `replay_report(traces, slots=V4的S, Ks=(4, 8, 16, 32))`。不跨配置合併，不做跨 run 擬合。
- **事先登記的選擇：** K* = 在所有 run × {50%, 75%} 中，最小平均 Δ 最大的那個 K。
- **淘汰（不用 GPU）：** K* 的最小平均 Δ < 0，或 other15 遊戲上的平均 Δ < +1 RHAE。

**Step 1（GPU：同步分池、同預算、按遊戲配對，2 個 replicate）**

- **設定：**
  - 同一個 server、同一時間跑兩個 arm，各 S/2 個 slots。
  - 每個 arm 各一份 public25 的全部遊戲，用獨立的 competition-mode scorecard。
  - 每個 replicate 隨機化遊戲的登記／admit 順序，兩個 arm 用同一順序。
  - window = 25 × 6,750 s ÷ (S/2)，使每個遊戲的平均 stream 預算等於 hidden 上的每遊戲預算。這個設計 **會** 量到 replay 量不到的「超過舊窗」上行。
- **arm：** A = 現行排程（每遊戲 cap 6,750 s，FIFO 接續）；B = `StallPool(K*)`。
- **成本：** 例如 S=24 時，window 14,062 s ≈ 3.9 h／replicate，2 個 replicate 共約 7.8 GPU-h。兩個 arm 共用 server，所以不需要兩倍 GPU。
- **主指標：** other15 的 15 個遊戲 × 2 個 replicate，共 30 對的平均配對 RHAE 差。
- **次指標：** dev10 配對差、通關數、park 次數、re-prefill 秒數。
- **淘汰：**
  - 平均 Δ < 0 → 淘汰。
  - 0 ≤ Δ < +1，或兩個 replicate 不同號 → 不花 Kaggle 提交，保留現狀。
  - re-prefill 超過 stream 秒數的 5%（replay 估計 ≤ 0.7%）→ 先修。
  - 平均 Δ ≥ +1 且兩個 replicate 都 > 0 → Step 2。

**Step 2（Kaggle）**

- 一次提交：現行 base（V4，或 V7 若 V7 勝出）加 pool，對比同 base。同模型、同 9 h。
- LB 單次雜訊約 ±3，所以這一步只防大幅退步：低於 base − 3 就回滾；≥ base 就保留。

---

## 8. 對照 2026 primary source／code：已有 vs 新增

| 來源 | 已有 | 這裡不同之處 |
|---|---|---|
| Franzen（M2 第 1，同模型同 GPU） | 110 遊戲、10 streams 的優先序排程 P=(A+B)·C，C 在 725 次 level attempts 上擬合；只在 context eviction 時交接；無單獨 ablation。 | **跨遊戲分配這個決策是他的。這不是新原理。** |
| rellik13（M2 第 3） | UCB 單一 pool，40 分鐘 time slice；認為它「主要是搬動停滯時間」。 | 搬動停滯時間是否有分數，這裡用精確 replay 回答：v1 +3.6～+4.1，v2 +0.25～+0.74。 |
| lordhansolo（M2 第 2） | 14 streams、無 admission control、每遊戲固定 3,918 s。 | 就是本文的 baseline 形式。 |

新增的部分只有三點：

1. 規則只有一個參數，不擬合任何 hazard 或存活模型，所以不依賴那些讓重開垮掉的分布假設。
2. 它的反事實能從未干預 log 精確計算；這是這個決策在同模型 trace 上第一個公開的同預算數字。
3. replay 與線上 gate 是同一個 class。

---

## 9. 你的逐 tick 證據候選

- 不列入這一輪。
- always-on 文字的公開紀錄是 null 或負：rellik13 的 per-action notes；Franzen 的 auto-timeline。
- 唯一被支持的形式是 transient-gated、按需取用。預期 +0～3 [假設]，而且只在 V4 只給最終幀時成立。
- 它改變模型輸入，所以無法用 log replay 驗算，必須做 GPU A/B。
- 它與 pool 的插點不同，可以之後另做一個 arm。
- palette 9 對 7 照修。

---

## Sources

- [adsdemaybe/arc-agi-3](https://github.com/adsdemaybe/arc-agi-3)：`runs/kaggle_submission-v1/benchmark.json`、`runs/kaggle_submission-v2/benchmark.json`，本文所有 [已執行] 數字的數據。
- [arc_agi](https://pypi.org/project/arc-agi/) 0.9.9 scorecard（per-level score、level weights、RESET 計數）。
- [Franzen M2 WRITEUP](https://github.com/da-fr/arc-agi-3-solution/blob/main/WRITEUP.md)；[tonghuikang/daniel-franzen-arc-agi-3](https://github.com/tonghuikang/daniel-franzen-arc-agi-3)（`kaggle/COMPARISON.md`）。
- [rellik13 M2 WRITEUP](https://github.com/LohitSiriki/arc-agi-3-milestone2-solution/blob/main/WRITEUP.md)。
- Fail-Fast／Restart-Smart（arXiv 2608.03222）：只讀到二手摘要；+5.2 與 +43.8% tokens 的數字取自你們的訊息。
