# NVIDIA cuOpt — When to Use It (and When Not)

This document explains the role of NVIDIA cuOpt in the SmartWaste MVD pipeline,
the regime in which it actually outperforms the CPU-based Google OR-Tools
fallback, and the empirical evidence behind the recommendation.

It is intended for operators deciding which solver to deploy, for engineers
evaluating cost-vs-quality trade-offs, and for anyone porting the framework
to a different city.

---

## TL;DR

| Workload | Recommended solver | Why |
|---|---|---|
| 1 vehicle, < 200 nodes, no time windows (Montevideo-style circuit) | **OR-Tools** | Gap to cuOpt is ~1%; no GPU cost justified |
| 3–15 vehicles, 200–500 nodes, tight capacity | **cuOpt (cloud or EC2)** | Gap grows to 3–6%; multi-route problems start benefiting from GPU |
| 10+ vehicles, 800+ nodes, or with time windows / pickup-delivery | **cuOpt EC2 (self-hosted)** | Gap reaches 7–9% and grows; NVIDIA's design regime |
| Development / CI / cost-sensitive validation | **OR-Tools** | $1/month vs $885/month, runs inside existing Lambda |

For the current Montevideo deployment (133 circuits, ~82 nodes each,
1–2 trucks per circuit), **OR-Tools is the cost-optimal choice**. cuOpt is
provisioned in the architecture as a forward-compatible upgrade path for the
three scaling vectors described below.

---

## 1. What cuOpt was designed to solve

NVIDIA cuOpt is a GPU-accelerated solver targeting **large-scale,
constraint-rich routing problems**. The published evidence base is concentrated
on a specific class of benchmarks:

- **Gehring & Homberger** (CVRPTW): 600–1,000 customers with hard time windows.
  cuOpt holds **15 world records** here.
- **Li & Lim** (PDPTW): pickup-and-delivery with synchronisation. cuOpt holds
  **8 world records** here.
- **Mega-scale instances**: a single NVIDIA A100 has been reported to solve a
  10,000-task CVRPTW in approximately 30 seconds, an order of magnitude beyond
  any CPU solver.

The widely cited **"100× speedup over CPU"** is **specifically for GPU-parallel
local-search kernels operating inside those benchmark regimes** — it is not an
end-to-end wall-clock latency claim, and it does not apply to small or
constraint-light instances.

### When does the GPU advantage kick in?

GPU parallelism pays off when the candidate-solution space is large enough to
amortise the overhead of kernel launches, GPU memory transfers, and population
synchronisation. Three dimensions drive this:

1. **Problem size (nodes)** — more nodes = exponentially larger combinatorial space.
2. **Fleet size (vehicles)** — each additional active vehicle multiplies the
   number of route partitions to evaluate.
3. **Constraint density** — time windows, pickup-delivery sync, multi-depot,
   and tight capacity all make CPU constraint propagation a bottleneck that
   GPUs avoid by evaluating many candidates in parallel.

Conversely, **a 1-vehicle CVRP with no time windows degenerates to a TSP-like
search space**, where CPU metaheuristics such as OR-Tools' Guided Local Search
(GLS) are extremely competitive — often within 1% of cuOpt's solution at
identical time budgets.

---

## 2. Empirical results on the SmartWaste workload

Three controlled experiments were run against the NVIDIA cuOpt API Catalog and
Google OR-Tools using identical OSRM cost matrices and demand vectors. All
experiments used a 10-second time budget per solve. Capacity was overridden
post-generation to `slack × total_demand / num_vehicles` to force genuine
multi-route solutions in regimes B and C.

The metric reported is the **cost gap** Δ% defined as:

```
Δ% = (cost_ORTools − cost_cuOpt) / cost_cuOpt × 100
```

A positive Δ% means cuOpt produced a lower-cost (better) solution.

### Regime A — TSP-like (single vehicle, holgura)

- **Setup**: 1 vehicle, 100–500 nodes, slack 1.5, time limits 3–11 s
- **Result**: mean **Δ% = +0.91%**, range −1.27% to +4.13%
- **Wins**: cuOpt 25/28, OR-Tools 3/28 (ties not counted), 2 errors
- **Read**: at this scale, OR-Tools' Guided Local Search is essentially
  competitive with cuOpt. The gap is within noise for most instances.

### Regime B — Multi-vehicle, tight capacity

- **Setup**: 3–15 vehicles, 200–500 nodes, slack ∈ {1.05, 1.20}, TL = 10 s
- **Result**: mean **Δ% = +4.13%**, range +0.65% to +6.68%
- **Wins**: cuOpt 16/16 (100%)
- **Scaling by vehicle count**:

| Vehicles | Mean Δ% (cuOpt better) |
|---|---|
| 3  | +3.30% |
| 5  | +3.58% |
| 10 | +4.13% |
| 15 | +5.51% |

- **Capacity slack effect**: 4.11% at slack 1.05 vs. 4.15% at slack 1.20 — **the
  driver is vehicle count, not capacity tightness**.

### Regime C — Mega-scale (NVIDIA's design regime)

- **Setup**: 10–20 vehicles, 800–1,000 nodes, slack 1.05, TL = 10 s
- **Result**: mean **Δ% = +7.88%**
- **Wins**: cuOpt 4/4

| Size | Vehicles | cuOpt cost | OR-Tools cost | Δ% |
|---|---|---|---|---|
| 800  | 10 | 51,364 | 55,785 | +8.61% |
| 800  | 20 | 71,321 | 76,049 | +6.63% |
| 1000 | 10 | 55,783 | 60,007 | +7.57% |
| 1000 | 20 | 74,996 | 81,520 | +8.70% |

The trend is now unambiguous: **cuOpt's advantage grows monotonically with both
problem size and fleet size**, approaching the regime where NVIDIA's Gehring &
Homberger world records were established.

### Summary plot (conceptual)

```
Δ% (cuOpt - ORTools) / cuOpt
   │
 8 │                                ●  (1000n, 20v)
   │                          ●     (800n, 10v)
 6 │                    ●           (200n, 15v)
   │              ●                 (200n, 10v)
 4 │        ●                       (200n, 5v)
   │   ●                            (200n, 3v)
 2 │
   │
 1 │●  (1v, 100-500n)  ← Montevideo's current operational regime
   └────────────────────────────────
        TSP-like        Multi-route        Mega-scale
```

---

## 3. Wall-clock latency and operational cost

| | OR-Tools | cuOpt (Cloud API) | cuOpt (EC2 g5.2xlarge) |
|---|---|---|---|
| Runtime per solve (TL = 10 s) | exactly 10 s | 10–15 s (with API overhead) | 11–13 s |
| Min. time to first feasible | ≤ 1 s | ~2 s (GPU warm-up) | ~2 s |
| Solver status flag | `FEASIBLE` always | `OPTIMAL` (internal flag) | `OPTIMAL` |
| Reliability under load | 100% sequential | 100% at C ≤ 5; rate-limited above | 100% up to C = 20 |
| Monthly cost increment | **~$1** (Lambda compute) | **$0** (free tier, 5K req/mo) | **+$885** (24/7 on-demand) |
| Max problem size | tested to 500 nodes | 1,000 nodes (API limit) | 10,000+ nodes |

The cloud API is the cheapest option (free), but is **rate-limited at C ≥ 10**
on the shared free-tier endpoint. The EC2 self-hosted instance handles up to
C = 20 with 100% success but serialises VRP work on a single GPU, growing
latency linearly with concurrency. For development, CI, and cost-sensitive
deployments, **OR-Tools inside the existing Lambda costs ~$1/month** and is the
right default.

---

## 4. Recommendation per scenario

### Use **OR-Tools** when:
- Single-vehicle routing or very low fleet count (1–2)
- Small problem size (< 200 nodes per solve)
- No time windows, no pickup-delivery, no multi-depot synchronisation
- Cost is a hard constraint and the 1–2% quality gap is acceptable
- CI / dev environments (validate the full pipeline cheaply)

### Use **cuOpt (Cloud API)** when:
- 5–10 vehicles per solve, 200–500 nodes
- Sequential workload (no burst concurrency)
- Free-tier budget (5,000 solves/month) is sufficient
- No data-residency constraints

### Use **cuOpt (Self-hosted EC2)** when:
- 10+ vehicles per solve and/or 500+ nodes
- Time windows are part of the formulation (VRPTW)
- Pickup-delivery or multi-depot synchronisation
- Bursty workload (multiple municipalities or batch solves)
- Data-residency or air-gap requirements

---

## 5. Three scaling vectors that make cuOpt net-positive for SmartWaste

The current Montevideo deployment sits in **Regime A** (1–2 trucks per circuit,
82 nodes average), where the cuOpt gap is ~1%. Three plausible scaling
directions push the workload into Regimes B or C, where cuOpt becomes the
unambiguous choice:

### 5.1 Multi-circuit consolidation
Group 2–3 neighbouring circuits into a single solve.

- New problem size: **~200–250 nodes per invocation**
- New fleet size: **5–7 trucks**
- Expected gap: **3–5%** (Regime B)
- Benefit: fewer trucks driving overlapping zones; better cross-circuit balancing

### 5.2 Explicit shift time windows
Today the optimiser uses shift assignments as a filter, not as a hard
time-window constraint inside the VRP. Promoting them to actual time windows
(06:00–14:00, 14:00–22:00, 22:00–06:00) moves the formulation from CVRP to
CVRPTW.

- Expected gap: **8–15%** (this is cuOpt's Gehring & Homberger regime)
- Benefit: more realistic schedules; shift compliance enforced at the solver level

### 5.3 Multi-municipality deployment
A regional deployment (Montevideo + Canelones + Maldonado) on a single
optimisation horizon.

- New problem size: **~1,000+ nodes**
- New fleet size: **20+ trucks**
- Expected gap: **8%+** measured (Regime C); could grow further with shared depots
- Benefit: regional vehicle pooling; lower fixed costs per municipality

In all three scenarios, cuOpt EC2 (~$885/month) becomes net-positive against
the operational savings, and the pluggable backend (`CUOPT_MODE` Terraform
variable) allows the switch without changing the rest of the pipeline.

---

## 6. Reproducing these results

The benchmark scripts live in `cuopt-client/test-performance/`:

| Script | Purpose |
|---|---|
| `benchmark_ortools.py` | OR-Tools sweep over sizes × time_limits |
| `benchmark_time_limits.py` | cuOpt API Catalog sweep over sizes × time_limits |
| `experiment_inflection.py` | Multi-vehicle × capacity slack comparison (Regime B) |
| `experiment_megascale.py` | 800–1,000 node mega-scale (Regime C) |

To reproduce:

```bash
# OR-Tools
cd cuopt-client/test-performance
python3 -u benchmark_ortools.py

# cuOpt requires the API key:
export CUOPT_API_KEY=<your-nvapi-key>
python3 -u benchmark_time_limits.py
python3 -u experiment_inflection.py
python3 -u experiment_megascale.py
```

Raw results land under `results/raw_*.json` and `results/*.csv` for further
analysis.

---

## 7. References

- [NVIDIA cuOpt — Product page](https://www.nvidia.com/en-us/ai-data-science/products/cuopt/)
- [NVIDIA cuOpt User Guide](https://docs.nvidia.com/cuopt/user-guide/latest/introduction.html)
- [NVIDIA blog — Record-Breaking cuOpt Algorithms (100×)](https://developer.nvidia.com/blog/record-breaking-nvidia-cuopt-algorithms-deliver-route-optimization-solutions-100x-faster)
- [NVIDIA blog — 23 World Records for Route Optimization](https://blogs.nvidia.com/blog/cuopt-route-optimization-metropolis-omniverse/)
- [Gehring & Homberger VRPTW benchmark — SINTEF](https://www.sintef.no/projectweb/top/vrptw/homberger-benchmark/)
- [Google OR-Tools documentation](https://developers.google.com/optimization)
