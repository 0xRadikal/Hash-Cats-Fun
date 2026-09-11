"""
feasibility.py — measure the REAL mint rate over a short live sample and
compute the probability that a viable quiet window occurs.

A viable window needs: no external mint for T seconds, where T is our solve
time at the difficulty that a burst=b implies. Since burst must first cool to
b (10s per step from current burst), and any mint resets burst upward, we
estimate using the observed inter-mint gap distribution.
"""
import time
import chain

RATE = 1_600_000  # 2-core C H/s


def sample_intermint(w3, c, seconds=90):
    """Watch totalMinted; record timestamps of new mints."""
    g = lambda fn: getattr(c.functions, fn)().call()
    t0 = time.time()
    last_total = g("totalMinted")
    mint_times = []
    while time.time() - t0 < seconds:
        tm = g("totalMinted")
        if tm != last_total:
            for _ in range(tm - last_total):
                mint_times.append(time.time())
            last_total = tm
        time.sleep(1.5)
    return mint_times


def main():
    w3, url = chain.connect()
    c = chain.get_contract(w3)
    print(f"Sampling live mint rate for 90s via {url} ...")
    ts = sample_intermint(w3, c, 90)
    if len(ts) < 2:
        print("Very few mints in 90s — network may be quiet! (good sign)")
        print(f"mints observed: {len(ts)}")
        return
    gaps = [ts[i+1]-ts[i] for i in range(len(ts)-1)]
    gaps.sort()
    n = len(gaps)
    mean = sum(gaps)/n
    p50 = gaps[n//2]
    p90 = gaps[min(n-1, int(n*0.9))]
    mx = gaps[-1]
    total_span = ts[-1]-ts[0]
    rate_per_min = len(ts)/ (total_span/60) if total_span else 0
    print(f"\nMints observed: {len(ts)} over {total_span:.0f}s "
          f"=> {rate_per_min:.1f} mints/min")
    print(f"Inter-mint gap: mean={mean:.1f}s p50={p50:.1f}s p90={p90:.1f}s max={mx:.1f}s")

    # Solve time needed at each burst level:
    print("\nRequired uninterrupted quiet window vs our solve time:")
    for burst in (0, 1, 2, 3):
        bits = 32 + burst
        solve = (2**bits)/RATE
        # crude Poisson: P(no mint for `solve` sec) = exp(-lambda*solve)
        import math
        lam = len(ts)/total_span if total_span else 0
        p = math.exp(-lam*solve) if lam else 1.0
        print(f"  burst={burst} ({bits}bit): need ~{solve/60:.0f}min quiet. "
              f"P(gap that long) ~ {p:.2e}")

    print("\nInterpretation: if these probabilities are tiny, brute quiet-window")
    print("hunting at current traffic is impractical; we must wait for the")
    print("project to genuinely cool down (fewer players).")


if __name__ == "__main__":
    main()
