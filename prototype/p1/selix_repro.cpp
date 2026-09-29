// 独立复现: 直接调用 SELIX(lit::Lit), 对比 "翻符号位编码" 与 "原始键" 两种情形
// 用法: selix_repro <flip:0|1> <n_bulk> <n_insert>
#include "lit/lit.h"
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <vector>
#include <algorithm>
#include <chrono>
#include <fstream>
#include <string>

static inline int64_t enc(int64_t v, bool flip) {
    return flip ? (int64_t)(((uint64_t)v) ^ 0x8000000000000000ULL) : v;
}
static long rss_mb() {
    std::ifstream f("/proc/self/status"); std::string l;
    while (std::getline(f, l)) if (l.rfind("VmRSS:", 0) == 0) return atol(l.c_str() + 6) / 1024;
    return -1;
}
int main(int argc, char** argv) {
    bool flip = atoi(argv[1]); int nb = atoi(argv[2]); int ni = atoi(argv[3]); int chk = argc > 4 ? atoi(argv[4]) : 10000; setvbuf(stdout, NULL, _IOLBF, 0);
    typedef std::pair<int64_t, uint64_t> KV;
    std::vector<KV> v; v.reserve(nb);
    for (int g = 1; g <= nb; g++) v.push_back({enc((int64_t)g * 2, flip), (uint64_t)g});
    std::sort(v.begin(), v.end(), [](const KV& a, const KV& b) { return a.first < b.first; });
    lit::Lit<int64_t, uint64_t> idx;
    idx.bulk_load(v.data(), (int)v.size());
    auto s = idx.get_stats();
    printf("[flip=%d] bulk_load n=%d data_nodes=%d model_nodes=%d rss=%ldMB\n", flip, nb, s.num_data_nodes, s.num_model_nodes, rss_mb());

    // (1) bulk_load 后逐键点查
    int notfound = 0, wrong = 0;
    for (int g = 1; g <= nb; g++) {
        auto it = idx.find(enc((int64_t)g * 2, flip));
        if (it.is_end()) notfound++; else if (it.payload() != (uint64_t)g) wrong++;
    }
    printf("[flip=%d] lookup after bulk_load: notfound=%d wrong_payload=%d of %d\n", flip, notfound, wrong, nb);

    // (2) 持续插入奇数键, 监控内存
    auto t0 = std::chrono::steady_clock::now();
    int done = 0;
    for (int g = 1; g <= ni; g++) {
        idx.insert(enc((int64_t)g * 2 + 1, flip), (uint64_t)(1000000000ULL + g));
        done++;
        if (g % chk == 0) {
            long r = rss_mb();
            auto st = idx.get_stats();
            printf("[flip=%d] inserted=%d rss=%ldMB data_nodes=%d expand_retrain=%d splits=%d\n", flip, g, r,
                   st.num_data_nodes, st.num_expand_and_retrains, st.num_sideways_splits + st.num_downward_splits);
            fflush(stdout);
            if (r > 1200) { printf("[flip=%d] ABORT: rss > 1200MB, 内存失控\n", flip); break; }
        }
    }
    double sec = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    printf("[flip=%d] insert done=%d in %.2fs (%.0f ops/s)\n", flip, done, sec, done / sec);

    // (3) 插入后点查新旧键
    notfound = wrong = 0;
    for (int g = 1; g <= done; g++) {
        auto it = idx.find(enc((int64_t)g * 2 + 1, flip));
        if (it.is_end()) notfound++; else if (it.payload() != (uint64_t)(1000000000ULL + g)) wrong++;
    }
    printf("[flip=%d] lookup of inserted keys: notfound=%d wrong_payload=%d of %d\n", flip, notfound, wrong, done);
    notfound = wrong = 0;
    for (int g = 1; g <= nb; g++) {
        auto it = idx.find(enc((int64_t)g * 2, flip));
        if (it.is_end()) notfound++; else if (it.payload() != (uint64_t)g) wrong++;
    }
    printf("[flip=%d] lookup of original keys after inserts: notfound=%d wrong_payload=%d of %d\n", flip, notfound, wrong, nb);
    return 0;
}
