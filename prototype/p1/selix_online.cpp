// 在线切换响应: 以默认参数 bulk_load, 运行中途切换密度档位, 观察每段的耗时/内存/SMO
// 用法: selix_online <init_d> <max_d> <min_d> <read_ratio> <switch_chunk> [chunks=10] [chunk_ops=200000]
#include "lit/lit.h"
#include <cstdio>
#include <cstdlib>
#include <vector>
#include <random>
#include <chrono>
int main(int argc, char** argv) {
    double di = atof(argv[1]), dx = atof(argv[2]), dn = atof(argv[3]), rr = atof(argv[4]);
    int sw = atoi(argv[5]); int chunks = argc > 6 ? atoi(argv[6]) : 10; int cops = argc > 7 ? atoi(argv[7]) : 200000;
    const int nb = 1000000;
    typedef std::pair<int64_t, uint64_t> KV;
    std::vector<KV> v; v.reserve(nb);
    for (int g = 1; g <= nb; g++) v.push_back({(int64_t)g * 2, (uint64_t)g});
    lit::Lit<int64_t, uint64_t> idx;
    idx.set_max_node_size(16 << 20);
    idx.set_density_params(0.70, 0.80, 0.60);
    idx.bulk_load(v.data(), nb);
    std::mt19937_64 rng(42);
    std::uniform_int_distribution<int64_t> kd(1, (int64_t)nb * 8);
    std::uniform_real_distribution<double> ud(0, 1);
    uint64_t sink = 0; int prev_smo = 0;
    for (int c = 1; c <= chunks; c++) {
        if (c == sw) idx.set_density_params(di, dx, dn);
        std::vector<std::pair<bool, int64_t>> ops(cops);
        for (auto& o : ops) { o.first = ud(rng) < rr; o.second = o.first ? (kd(rng) % nb + 1) * 2 : kd(rng) * 2 + 1; }
        auto t0 = std::chrono::steady_clock::now();
        for (auto& o : ops) { if (o.first) { auto it = idx.find(o.second); if (!it.is_end()) sink += it.payload(); } else idx.insert(o.second, 7); }
        double sec = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
        auto s = idx.get_stats();
        int smo = s.num_expand_and_scales + s.num_expand_and_retrains + s.num_downward_splits + s.num_sideways_splits;
        printf("%s%.3f/%.1f/%d", c == 1 ? "" : "  ", sec * 1e6 / cops, (idx.data_size() + idx.model_size()) / 1048576.0, smo - prev_smo);
        prev_smo = smo;
    }
    printf("   (sink=%lu)\n", (unsigned long)(sink & 1));
    return 0;
}
