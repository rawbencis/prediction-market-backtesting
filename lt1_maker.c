/* lt1_maker.c: C port of dsim_strict.maker_strict (overnight-wt/dsim_strict.py), line for line, plus one addition:
 * `used` (per print, shares already taken by our own earlier simulated orders in the same world). A print offers
 * sz - used; whatever this order takes is added to used. With used all zero the result equals maker_strict exactly
 * (checked by lt1_sim.py v0). Build: gcc -O2 -shared -fPIC -o lt1_maker.so lt1_maker.c
 * Prices are 1e-4 $ ints in UP terms. model: 0 pess, 1 prop, 2 front. Returns number of fills, or -1 nobook, -2 rejected. */
#include <stdint.h>

static int64_t ss_right(const int64_t *a, int64_t n, int64_t v) { /* numpy searchsorted(a, v, 'right') */
    int64_t lo = 0, hi = n;
    while (lo < hi) { int64_t m = (lo + hi) / 2; if (a[m] <= v) lo = m + 1; else hi = m; }
    return lo;
}

int lt1_maker(const int64_t *bx, const int32_t *bb, const int32_t *bk, const float *bbs, const float *bks, int64_t nb,
              const int64_t *px_t, const uint8_t *phb, const int32_t *pup, const double *psz, double *used, int64_t npr,
              int64_t t_arr, int tok, int32_t price, double size, int64_t t_cxl, int model,
              int64_t *ox, double *osz, double *oah, int maxf) {
    int s = tok == 0 ? 0 : 1;
    int32_t q = tok == 0 ? price : 10000 - price;
#define BETTER(a, c) (s == 0 ? ((a) > (c)) : ((a) < (c)))
    int64_t i = ss_right(bx, nb, t_arr) - 1;
    if (i < 0 || (bb[i] == 0 && bk[i] == 0)) return -1;
    int32_t best = s == 0 ? bb[i] : bk[i], opp = s == 0 ? bk[i] : bb[i];
    if (opp && (s == 0 ? q >= opp : q <= opp)) return -2;
    int ahead_known, disp_known;
    double ahead = 0, disp = 0;
    if (best == 0 || BETTER(q, best)) { ahead = 0; disp = 0; ahead_known = disp_known = 1; }
    else if (q == best) { disp = s == 0 ? bbs[i] : bks[i]; ahead = disp; ahead_known = disp_known = 1; }
    else { ahead_known = disp_known = 0; }
    if (model == 2) { ahead = 0; ahead_known = 1; }
    double left = size, since = 0;
    int nf = 0;
#define FILL(X, N, AHK, AH, J) do { double n_ = (N); if (n_ > left) n_ = left; \
        if (n_ > 1e-9) { left -= n_; if ((J) >= 0) used[(J)] += n_; \
            if (nf < maxf) { ox[nf] = (X); osz[nf] = n_; oah[nf] = (AHK) ? (AH) : -1.0; } nf++; } } while (0)
    int64_t jb = i + 1, jp = ss_right(px_t, npr, t_arr);
    while (left > 1e-9) {
        int hb_ok = jb < nb && bx[jb] < t_cxl;
        int hp_ok = jp < npr && px_t[jp] <= t_cxl;
        if (!hb_ok && !hp_ok) break;
        if (hp_ok && (!hb_ok || px_t[jp] <= bx[jb])) {
            int64_t j = jp++;
            if ((phb[j] != 0) != (s == 0)) continue;
            int32_t px = pup[j];
            double n = psz[j] - used[j];
            if (n < 0) n = 0;
            if (BETTER(q, px)) {
                double a0 = ahead_known ? ahead : 0.0;
                double d = a0 < n ? a0 : n;
                if (ahead_known) ahead -= d;
                FILL(px_t[j], n - d, 1, a0, j);
            } else if (px == q) {
                since += n;
                if (!ahead_known) {
                    if (model == 2) { ahead = 0; ahead_known = 1; }
                    else if (disp_known) { ahead = disp; ahead_known = 1; }
                }
                if (!ahead_known) continue;
                double a0 = ahead;
                double d = ahead < n ? ahead : n;
                ahead -= d;
                FILL(px_t[j], n - d, 1, a0, j);
            }
            continue;
        }
        int64_t j = jb++;
        if (bb[j] == 0 && bk[j] == 0) { disp_known = 0; since = 0; continue; }
        int32_t nbest = s == 0 ? bb[j] : bk[j], nopp = s == 0 ? bk[j] : bb[j];
        double nsz = s == 0 ? bbs[j] : bks[j];
        if (nopp && (s == 0 ? q >= nopp : q <= nopp)) {
            double oz = s == 0 ? bks[j] : bbs[j];
            FILL(bx[j], oz - (ahead_known ? ahead : 0.0), ahead_known, ahead, -1);
        }
        if (nbest == 0 || BETTER(q, nbest)) { ahead = 0; disp = 0; ahead_known = disp_known = 1; }
        else if (nbest == q) {
            if (model == 2) { ahead = 0; ahead_known = 1; }
            else if (!ahead_known) { ahead = nsz; ahead_known = 1; }
            else if (disp_known && model == 1) {
                double cxl = disp - nsz - since, D = disp - since;
                if (cxl > 0 && D > 0) { ahead = ahead - cxl * ahead / D; if (ahead < 0) ahead = 0; }
            }
            if (ahead > nsz) ahead = nsz;
            disp = nsz; disp_known = 1;
        } else { disp_known = 0; }
        since = 0;
    }
    return nf;
}
