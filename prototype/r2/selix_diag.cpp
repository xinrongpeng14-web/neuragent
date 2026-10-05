// Why does c_idx spike under the dense preset, and what does compaction buy?
// Standalone SELIX (lit) measurement with log-normal keys as in the experiment.
//
//   selix_diag <init_d> <max_d> <min_d> <read_ratio> <chunks> <chunk_ops> [compact_init_d]
//
// Prints one line per chunk: us/op, memory (MB), SMO breakdown since the previous
// chunk, and the per-node counters aggregated over all data nodes (shifts per
// insert, exponential-search iterations per operation, fill ratio).
// With compact_init_d the index is rebuilt by bulk_load at that density after the
// chunks, then read latency, memory and one more write chunk are measured.
#include "lit/lit.h"
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <vector>
#include <random>
#include <chrono>
#include <algorithm>
#include <unordered_set>

typedef lit::Lit<int64_t, uint64_t> Index;
typedef std::pair<int64_t, uint64_t> KV;
static const int64_t MAX_KEY = (1LL << 53) - 1;

struct Agg { long long shifts = 0, iters = 0, inserts = 0, lookups = 0, keys = 0, capacity = 0; int nodes = 0; };

static Agg aggregate(const Index& idx) {
    Agg a;
    Index::NodeIterator it(&idx);
    for (auto* n = it.current(); n; n = it.next()) {
        if (!n->is_leaf_) continue;
        auto* d = static_cast<Index::data_node_type*>(n);
        a.nodes++; a.shifts += d->num_shifts_; a.iters += d->num_exp_search_iterations_;
        a.inserts += d->num_inserts_; a.lookups += d->num_lookups_; a.keys += d->num_keys_; a.capacity += d->data_capacity_;
    }
    return a;
}

static double now() { return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count(); }
static double mb(const Index& idx) { return (idx.data_size() + idx.model_size()) / 1048576.0; }

struct Keys {
    std::mt19937_64 rng; std::lognormal_distribution<double> ln; std::unordered_set<int64_t> seen;
    Keys(uint64_t seed) : rng(seed), ln(0.0, 2.0) {}
    int64_t draw() { double v = std::round(ln(rng) * 1e9); if (v < 1) v = 1; if (v > (double)MAX_KEY) v = (double)MAX_KEY; return (int64_t)v; }
    int64_t fresh() { for (;;) { int64_t k = draw(); if (seen.insert(k).second) return k; } }
};

static void print_header() {
    printf("%-6s %7s %8s | %5s %5s %5s %5s %7s %7s | %6s %6s %5s %6s\n", "chunk", "us/op", "mem_MB",
           "exp&s", "exp&r", "splD", "splS", "splt_ms", "cost_ms", "shf/in", "it/op", "fill", "nodes");
}

static void run_chunk(Index& idx, Keys& K, std::vector<int64_t>& lookup_keys, double read_ratio, int ops,
                      const char* label, Index::Stats& prev, Agg& prevA) {
    std::uniform_real_distribution<double> ud(0, 1);
    std::uniform_int_distribution<size_t> pick(0, lookup_keys.size() - 1);
    std::vector<std::pair<bool, int64_t>> plan(ops);
    for (auto& o : plan) { o.first = ud(K.rng) < read_ratio; o.second = o.first ? lookup_keys[pick(K.rng)] : K.fresh(); }
    uint64_t sink = 0;
    double t0 = now();
    for (auto& o : plan) {
        if (o.first) { auto it = idx.find(o.second); if (!it.is_end()) sink += it.payload(); }
        else idx.insert(o.second, 7);
    }
    double sec = now() - t0;
    const auto& s = idx.get_stats();
    Agg a = aggregate(idx);
    long long dins = a.inserts - prevA.inserts, dops = (a.inserts + a.lookups) - (prevA.inserts + prevA.lookups);
    printf("%-6s %7.3f %8.1f | %5d %5d %5d %5d %7.1f %7.1f | %6.1f %6.2f %5.2f %6d\n", label, sec * 1e6 / ops, mb(idx),
           s.num_expand_and_scales - prev.num_expand_and_scales, s.num_expand_and_retrains - prev.num_expand_and_retrains,
           s.num_downward_splits - prev.num_downward_splits, s.num_sideways_splits - prev.num_sideways_splits,
           (s.splitting_time - prev.splitting_time) / 1e6, (s.cost_computation_time - prev.cost_computation_time) / 1e6,   // stats are in ns
           dins ? (double)(a.shifts - prevA.shifts) / dins : 0.0, dops ? (double)(a.iters - prevA.iters) / dops : 0.0,
           a.capacity ? (double)a.keys / a.capacity : 0.0, a.nodes);
    fflush(stdout);
    prev = s; prevA = a;
    if (sink == 1) printf("");
}

static double read_latency_us(Index& idx, std::vector<int64_t>& keys, std::mt19937_64& rng, int n) {
    std::uniform_int_distribution<size_t> pick(0, keys.size() - 1);
    std::vector<int64_t> q(n); for (auto& k : q) k = keys[pick(rng)];
    uint64_t sink = 0; double t0 = now();
    for (auto k : q) { auto it = idx.find(k); if (!it.is_end()) sink += it.payload(); }
    double us = (now() - t0) * 1e6 / n; if (sink == 1) printf(""); return us;
}

int main(int argc, char** argv) {
    if (argc < 7) { fprintf(stderr, "usage: %s init max min read_ratio chunks chunk_ops [compact_init]\n", argv[0]); return 1; }
    double di = atof(argv[1]), dx = atof(argv[2]), dn = atof(argv[3]), rr = atof(argv[4]);
    int chunks = atoi(argv[5]), cops = atoi(argv[6]);
    double compact = argc > 7 ? atof(argv[7]) : 0.0;
    const int nb = 1000000;
    Keys K(20260929);
    std::vector<KV> v; v.reserve(nb);
    for (int i = 0; i < nb; i++) v.push_back({K.fresh(), (uint64_t)i});
    std::sort(v.begin(), v.end());
    std::vector<int64_t> lookup_keys; lookup_keys.reserve(nb);
    for (auto& kv : v) lookup_keys.push_back(kv.first);

    Index idx;
    idx.set_max_node_size(16 << 20);
    idx.set_density_params(di, dx, dn);
    double t0 = now(); idx.bulk_load(v.data(), nb); double build = now() - t0;
    printf("preset init/max/min = %.2f/%.2f/%.2f  read_ratio %.2f  bulk_load %.2f s  %.1f MB  %d keys\n", di, dx, dn, rr, build, mb(idx), (int)idx.size());
    print_header();
    Index::Stats prev = idx.get_stats(); Agg prevA = aggregate(idx);
    char label[16];
    for (int c = 1; c <= chunks; c++) { snprintf(label, sizeof label, "w%d", c); run_chunk(idx, K, lookup_keys, rr, cops, label, prev, prevA); }
    printf("after writes: %d keys, %.1f MB, read latency %.3f us\n", (int)idx.size(), mb(idx), read_latency_us(idx, lookup_keys, K.rng, 1000000));

    if (compact > 0) {
        // compaction: rebuild from the index's own keys with bulk_load at the compaction density
        std::vector<KV> all; all.reserve(idx.size());
        for (auto it = idx.begin(); !it.is_end(); ++it) all.push_back({it.key(), it.payload()});
        Index* fresh = new Index();
        fresh->set_max_node_size(16 << 20);
        fresh->set_density_params(compact, std::min(0.99, compact + 0.05), std::max(0.1, compact - 0.10));
        t0 = now(); fresh->bulk_load(all.data(), (int)all.size()); double rebuild = now() - t0;
        printf("compaction at init density %.2f: rebuild %.2f s for %d keys, memory %.1f -> %.1f MB (%.0f%%), read latency %.3f -> %.3f us\n",
               compact, rebuild, (int)all.size(), mb(idx), mb(*fresh), 100.0 * mb(*fresh) / mb(idx),
               read_latency_us(idx, lookup_keys, K.rng, 1000000), read_latency_us(*fresh, lookup_keys, K.rng, 1000000));
        // writes resume on the compacted index with the ORIGINAL preset
        fresh->set_density_params(di, dx, dn);
        Index::Stats p2 = fresh->get_stats(); Agg a2 = aggregate(*fresh);
        print_header();
        for (int c = 1; c <= 3; c++) { snprintf(label, sizeof label, "c+w%d", c); run_chunk(*fresh, K, lookup_keys, rr, cops, label, p2, a2); }
        printf("after 3 more write chunks on the compacted index: %.1f MB\n", mb(*fresh));
        delete fresh;
    }
    return 0;
}
