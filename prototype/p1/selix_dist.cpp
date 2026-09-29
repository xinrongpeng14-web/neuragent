// 键分布 × 档位 × 读写比: 运行中第 4 段切换档位, 报告切换后(第5-10段)的平均耗时、末尾内存、切换后SMO
// 用法: selix_dist <uniform|lognormal> <init_d> <max_d> <min_d> <read_ratio>
#include "lit/lit.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <random>
#include <chrono>
#include <algorithm>
#include <unordered_set>
int main(int argc, char** argv) {
    bool logn = !strcmp(argv[1], "lognormal");
    double di = atof(argv[2]), dx = atof(argv[3]), dn = atof(argv[4]), rr = atof(argv[5]);
    const int nb = 1000000, chunks = 10, cops = 200000, sw = 4;
    std::mt19937_64 rng(7);
    std::vector<int64_t> keys; keys.reserve(nb + chunks * cops + 1000);
    std::unordered_set<int64_t> seen;
    std::lognormal_distribution<double> ln(0.0, 2.0);
    std::uniform_int_distribution<int64_t> un(1, (int64_t)16000000);
    while ((int)keys.size() < nb + chunks * cops) {
        int64_t k = logn ? (int64_t)(ln(rng) * 1e9) : un(rng);
        if (k > 0 && seen.insert(k).second) keys.push_back(k);
    }
    typedef std::pair<int64_t, uint64_t> KV;
    std::vector<KV> v; v.reserve(nb);
    for (int i = 0; i < nb; i++) v.push_back({keys[i], (uint64_t)i});
    std::sort(v.begin(), v.end());
    lit::Lit<int64_t, uint64_t> idx;
    idx.set_max_node_size(16 << 20);
    idx.set_density_params(0.70, 0.80, 0.60);
    idx.bulk_load(v.data(), nb);
    std::uniform_real_distribution<double> ud(0, 1);
    std::uniform_int_distribution<int> pick(0, nb - 1);
    size_t next = nb; uint64_t sink = 0; double tsum = 0; int smo_at_sw = 0; double tpre = 0;
    for (int c = 1; c <= chunks; c++) {
        if (c == sw) { idx.set_density_params(di, dx, dn); auto s = idx.get_stats();
            smo_at_sw = s.num_expand_and_scales + s.num_expand_and_retrains + s.num_downward_splits + s.num_sideways_splits; }
        std::vector<std::pair<bool, int64_t>> ops(cops);
        for (auto& o : ops) { o.first = ud(rng) < rr; o.second = o.first ? keys[pick(rng)] : keys[next++]; }
        auto t0 = std::chrono::steady_clock::now();
        for (auto& o : ops) { if (o.first) { auto it = idx.find(o.second); if (!it.is_end()) sink += it.payload(); } else idx.insert(o.second, 7); }
        double sec = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
        if (c > sw) tsum += sec; else if (c < sw) tpre += sec;
    }
    auto s = idx.get_stats();
    int smo = s.num_expand_and_scales + s.num_expand_and_retrains + s.num_downward_splits + s.num_sideways_splits;
    printf("%.3f %.1f %d %.3f %lu\n", tsum * 1e6 / ((chunks - sw) * (double)cops), (idx.data_size() + idx.model_size()) / 1048576.0, smo - smo_at_sw,
           tpre * 1e6 / ((sw - 1) * (double)cops), (unsigned long)(sink & 1));
    return 0;
}
