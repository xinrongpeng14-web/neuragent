// SELIX 参数敏感性: 三档参数 × 两种读写比, 测索引层吞吐与内存 (脱离 PG)
// 用法: selix_params <node_mb> <init_d> <max_d> <min_d> <n_bulk> <n_ops> <read_ratio>
#include "lit/lit.h"
#include <cstdio>
#include <cstdlib>
#include <vector>
#include <random>
#include <chrono>
int main(int argc, char** argv) {
    int node_mb = atoi(argv[1]); double di = atof(argv[2]), dx = atof(argv[3]), dn = atof(argv[4]);
    int nb = atoi(argv[5]), nops = atoi(argv[6]); double rr = atof(argv[7]);
    typedef std::pair<int64_t, uint64_t> KV;
    std::vector<KV> v; v.reserve(nb);
    for (int g = 1; g <= nb; g++) v.push_back({(int64_t)g * 2, (uint64_t)g});
    lit::Lit<int64_t, uint64_t> idx;
    idx.set_max_node_size(node_mb << 20);
    idx.set_density_params(di, dx, dn);
    idx.bulk_load(v.data(), nb);
    double mem0 = (idx.data_size() + idx.model_size()) / 1048576.0;
    std::mt19937_64 rng(42);
    std::uniform_int_distribution<int64_t> kd(1, (int64_t)nb * 4);
    std::uniform_real_distribution<double> ud(0, 1);
    std::vector<std::pair<bool, int64_t>> ops(nops);
    for (auto& o : ops) { o.first = ud(rng) < rr; o.second = o.first ? (kd(rng) % nb + 1) * 2 : kd(rng) * 2 + 1; }
    uint64_t sink = 0;
    auto t0 = std::chrono::steady_clock::now();
    for (auto& o : ops) {
        if (o.first) { auto it = idx.find(o.second); if (!it.is_end()) sink += it.payload(); }
        else idx.insert(o.second, 7);
    }
    double sec = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    auto s = idx.get_stats();
    double mem1 = (idx.data_size() + idx.model_size()) / 1048576.0;
    int smo = s.num_expand_and_scales + s.num_expand_and_retrains + s.num_downward_splits + s.num_sideways_splits;
    printf("node=%2dMB d=%.2f/%.2f/%.2f read=%.1f | %8.0f ops/s  %.3f us/op | mem %5.1f -> %5.1f MB | data_nodes=%d smo=%d (sink=%lu)\n",
           node_mb, di, dx, dn, rr, nops / sec, sec * 1e6 / nops, mem0, mem1, s.num_data_nodes, smo, (unsigned long)(sink & 1));
    return 0;
}
