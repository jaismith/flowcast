"""Numba SNOW-17 kernel, vectorized over HRUs and sequential in time.

Ported from NOAA-OWP snow17 (https://github.com/NOAA-OWP/snow17, Apache-2.0): PACK19, MELT19, AESC19, ROUT19
and SNDEPTH/SNEW/SNOWT/SNOWPACK. With classic flags it reproduces the OWP Fortran (tests/test_snow17_reference.py).

Deviations from the OWP build, all deliberate:
- SNOF (new-snow threshold for leaving the depletion curve) is a parameter; OWP leaves it uninitialized (0).
- TPREV is updated every step; OWP never updates it (only snow depth depends on it).
- Zero-depth divisions in SNDEPTH are guarded.

Extensions (selected by `flags`):
- melt_mode=RADIATION: non-rain melt = tf*(Ta-MBASE)+ + srf*(1-albedo)*SW*dt/Lf when Ta > rad_tmin, with an
  age-based albedo. The absorbed-shortwave term is also added to rain-on-snow melt.
- rain_on_snow=HUMIDITY_WIND: the rain-on-snow energy balance uses the forcing vapor pressure (capped at
  saturation) instead of 90% RH, and UADJ = max(wind_function * u_eff, uadj_min) instead of a constant.
- The rain/snow split is precomputed outside the kernel (wet-bulb or air temperature) and passed as `fracs`.
"""

import math

import numpy as np
from numba import njit, prange

LF_J_PER_KG = 334000.0

# State vector layout per HRU.
WE, NEGHS, LIQW, TINDEX, ACCMAX, SB, SBAESC, SBWS, STORGE, AEADJ = range(10)
EXLAG0 = 10  # EXLAG(1..7) at 10..16
SNDPT, SNTMP, TPREV, ALB_AGE = 17, 18, 19, 20
N_STATE = 21

# Output layout: out[k, t, h].
OUTPUTS = (
    "swe", "rain_plus_melt", "melt", "snowfall", "rainfall", "snow_cover_frac", "cold_content",
    "liquid_water", "snow_depth", "albedo", "ros_melt", "rain_on_snow",
)  # fmt: skip
N_OUT = len(OUTPUTS)

# Parameter columns (params.KERNEL_PARAM_NAMES).
(P_SCF, P_MFMAX, P_MFMIN, P_UADJ, P_SI, P_NMF, P_TIPM, P_MBASE, P_PLWHC, P_DAYGM, P_SNOF, P_TF, P_SRF,
 P_RADTMIN, P_AFRESH, P_AOLD, P_ATAU, P_AREF, P_WFUN, P_UADJMIN, P_LAT, P_FOREST, P_CWIND, P_CSW) = range(24)  # fmt: skip


@njit(cache=True, error_model="numpy")
def _aesc19(twe, accmax, sb, sbaesc, sbws, si, adc, aeadj, snof):
    if twe > accmax:
        accmax = twe
    if twe >= aeadj:
        aeadj = 0.0
    ai = accmax
    if accmax > si:
        ai = si
    if aeadj > 0.0:
        ai = aeadj
    if twe >= ai:
        sb = twe
        sbws = twe
        aesc = 1.0
    elif twe <= sb:
        r = (twe / ai) * 10.0 + 1.0
        n = int(r)
        r = r - n
        aesc = adc[n - 1] + (adc[n] - adc[n - 1]) * r
        if aesc > 1.0:
            aesc = 1.0
        sb = twe + snof
        sbws = twe
        sbaesc = aesc
    elif twe >= sbws:
        aesc = 1.0
    else:
        aesc = sbaesc + (1.0 - sbaesc) * ((twe - sb) / (sbws - sb))
    if aesc < 0.05:
        aesc = 0.05
    if aesc > 1.0:
        aesc = 1.0
    return aesc, accmax, sb, sbaesc, sbws, aeadj


@njit(cache=True, error_model="numpy")
def _rout19(it, excess, we, aesc, storge, nexlag, exlag):
    fit = float(it)
    packro = 0.0
    cl = 0.03 * fit / 6.0
    if excess != 0.0:
        if excess < 0.1 or we < 1.0:
            exlag[0] += excess
        else:
            n = int((excess * 4.0) ** 0.3 + 0.5)
            if n == 0:
                n = 1
            fn = float(n)
            for i in range(1, n + 1):
                term = cl * we * fn / (excess * (i - 0.5))
                if term > 150.0:
                    term = 150.0
                flag = 5.33 * (1.0 - math.exp(-term))
                l2 = int((flag + fit) / fit + 1.0)
                l1 = l2 - 1
                por2 = (flag + fit - l1 * it) / fit
                por1 = 1.0 - por2
                exlag[l2 - 1] += por2 * excess / fn
                exlag[l1 - 1] += por1 * excess / fn
    if storge + exlag[0] != 0.0:
        if storge + exlag[0] < 0.1:
            packro = storge + exlag[0]
            storge = 0.0
        else:
            el = exlag[0] / fit
            els = el / (25.4 * aesc)
            wes = we / (25.4 * aesc)
            term = 500.0 * els / (wes**1.3)
            if term > 150.0:
                term = 150.0
            r1 = 1.0 / (5.0 * math.exp(-term) + 1.0)
            for _ in range(it):
                os = (storge + el) * r1
                packro += os
                storge = storge + el - os
            if storge <= 0.001:
                packro += storge
                storge = 0.0
    for i in range(1, nexlag):
        exlag[i - 1] = exlag[i]
    exlag[nexlag - 1] = 0.0
    return packro, storge


@njit(cache=True, error_model="numpy")
def _melt19(idn, alat, ta, mfmax, mfmin, mbase, tindex, tipm, nmf):
    diff = mfmax - mfmin
    dayn = float(idn)
    if alat < 54.0:
        mf = math.sin(dayn * 2.0 * 3.1416 / 366.0) * diff * 0.5 + (mfmax + mfmin) * 0.5
    else:
        if idn >= 275:
            x = (dayn - 275.0) / (458.0 - 275.0)
        elif idn >= 92:
            x = (275.0 - dayn) / (275.0 - 92.0)
        else:
            x = (91.0 + dayn) / 183.0
        xx = math.sin(dayn * 2.0 * 3.1416 / 366.0) * 0.5 + 0.5
        if x <= 0.48:
            adjmf = 0.0
        elif x >= 0.70:
            adjmf = 1.0
        else:
            adjmf = (x - 0.48) / (0.70 - 0.48)
        mf = (xx * adjmf) * diff + mfmin
    ratio = mf / mfmax
    tmx = max(ta - mbase, 0.0)
    tsur = min(ta, 0.0)
    cnhs = ratio * nmf * (tindex - tsur)
    tindex = min(tindex + tipm * (ta - tindex), 0.0)
    melt = mf * tmx if tmx > 0.0 else 0.0
    return melt, cnhs, tindex


@njit(cache=True, error_model="numpy")
def _snowt(sh, ds, we, sliq, dta, tsnow, dhc):
    shx = 0.01 * sh
    dhcx = 0.01 * dhc
    stot = we + sliq
    dst = 0.1 * stot / sh
    sl = 0.0442 * math.exp(5.181 * dst)
    fl = sliq / stot
    sc = 2.1e6 * ds + 1e3 * (1.0 - ds - fl) + 4.2e6 * fl
    alp = math.sqrt(3.14 * sc / (43200.0 * sl))
    if dhc > 0.0:
        tsnow = tsnow + dta * ((math.exp(-alp * dhcx) - math.exp(-alp * shx)) / (alp * (shx - dhcx)))
    else:
        tsnow = tsnow + dta * ((1.0 - math.exp(-alp * shx)) / (alp * shx))
    return min(tsnow, 0.0)


@njit(cache=True, error_model="numpy")
def _snowpack(w, dts, hc, ds, sliq, dfall, srfrz, tsnow):
    wx = w * 0.1
    dsc = 1.0
    if wx > 1e-2:
        b = dts * 0.01 * math.exp(0.08 * tsnow - 21.0 * ds)
        dsc = (math.exp(b * wx) - 1.0) / (b * wx)
    a = 0.01
    if sliq > 0.0:
        a = a * 2.0
    c = 0.04 * tsnow
    if ds > 0.20:
        c = c - 46.0 * (ds - 0.20)
    dsm = math.exp(a * dts * math.exp(c))
    dsx = ds * dsc * dsm
    if dsx > 0.45:
        dsx = 0.45
    if dsx < 0.05:
        dsx = ds
    ds = dsx
    dwx = wx - 0.1 * (dfall + srfrz)
    if dwx > 0.0:
        hc = dwx / ds
    elif wx > 0.0:
        hc = wx / dsx
    else:
        hc = 0.0
    return hc, ds


@njit(cache=True, error_model="numpy")
def _sndepth(we, sliq, dfall, sgslos, srfrz, ta, dta, idt, sh, ds, tsnow):
    dt = float(idt)
    dhc = 0.0
    sdn = 0.0
    tsnew = ta
    if dfall > 0.0:
        px = 0.1 * dfall
        sdn = 0.05 if ta <= -15.0 else 0.05 + 0.0017 * (ta + 15.0) ** 1.5
        dhc = px / sdn
        tsnew = _snowt(dhc, sdn, dfall, 0.0, dta, tsnew, 0.0)
        dhc, sdn = _snowpack(dfall, dt, dhc, sdn, 0.0, 0.0, 0.0, tsnew)
    if sh > 0.0001:
        shn = dhc + sh
        dsn = (sdn * dhc + ds * sh) / shn
        tsnow = _snowt(shn, dsn, we, sliq, dta, tsnow, dhc)
        sh, ds = _snowpack(we, dt, sh, ds, sliq, dfall, srfrz, tsnow)
        if sgslos > 0.0:
            sh = sh - 0.1 * sgslos / ds
        if sh < 0.0:
            sh = 0.0
        if sh + dhc > 0.0:
            tsnow = (tsnow * sh + tsnew * dhc) / (sh + dhc)
        sh = sh + dhc
    else:
        sh = dhc
        ds = sdn
        if sgslos > 0.0 and ds > 0.0:
            sh = sh - 0.1 * sgslos / ds
        if sh < 0.0:
            sh = 0.0
        tsnow = tsnew
    if sh > 0.0:
        ds = 0.1 * we / sh
        if ds > 0.45:
            ds = 0.45
            sh = 0.1 * we / ds
    else:
        ds = 0.45
        sh = 0.1 * we / ds
    return sh, ds, tsnow


@njit(cache=True, error_model="numpy")
def _esat_anderson(ta):
    return 2.7489e8 * math.exp(-4278.63 / (ta + 242.792))


@njit(parallel=True, cache=True, error_model="numpy")
def snow17_kernel(ta, px, fracs, ea, pa, wind, sw, idn, idt, pv, adc, flags, state, out):
    """Advance every HRU through all time steps.

    ta degC, px mm per step, fracs snow fraction, ea vapor pressure mb, pa pressure mb, wind m/s,
    sw open-sky terrain-corrected shortwave W/m2: all (time, hru). idn (time,) day number from March 21.
    pv (hru, n_param) parameters, state (hru, N_STATE) updated in place, out (N_OUT, time, hru) float32.
    """
    nt, nh = ta.shape
    melt_mode = flags[0]
    ros_mode = flags[2]
    fit = float(idt)
    nexlag = 5 // idt + 2
    for h in prange(nh):
        scf = pv[h, P_SCF]
        mfmax = pv[h, P_MFMAX] * fit / 6.0
        mfmin = pv[h, P_MFMIN] * fit / 6.0
        uadj_c = pv[h, P_UADJ] * fit / 6.0
        si = pv[h, P_SI]
        nmf = pv[h, P_NMF] * fit / 6.0
        tipm = 1.0 - (1.0 - pv[h, P_TIPM]) ** (fit / 6.0)
        mbase = pv[h, P_MBASE]
        plwhc = pv[h, P_PLWHC]
        gm = pv[h, P_DAYGM] * fit / 24.0
        snof = pv[h, P_SNOF] * fit
        tf = pv[h, P_TF] * fit
        srf = pv[h, P_SRF]
        rad_tmin = pv[h, P_RADTMIN]
        a_fresh = pv[h, P_AFRESH]
        a_old = pv[h, P_AOLD]
        a_tau = pv[h, P_ATAU]
        a_ref = pv[h, P_AREF]
        wfun = pv[h, P_WFUN]
        uadj_min = pv[h, P_UADJMIN]
        alat = pv[h, P_LAT]
        ff = pv[h, P_FOREST]
        wind_red = 1.0 - ff * (1.0 - pv[h, P_CWIND])
        sw_red = 1.0 - ff * (1.0 - pv[h, P_CSW])
        sfnew = 1.5 * fit
        rfmin = 0.25 * fit
        sbci = 0.0612 * fit
        rad_mm = fit * 3600.0 / LF_J_PER_KG

        we = state[h, WE]
        neghs = state[h, NEGHS]
        liqw = state[h, LIQW]
        tindex = state[h, TINDEX]
        accmax = state[h, ACCMAX]
        sb = state[h, SB]
        sbaesc = state[h, SBAESC]
        sbws = state[h, SBWS]
        storge = state[h, STORGE]
        aeadj = state[h, AEADJ]
        exlag = np.zeros(7)
        for i in range(7):
            exlag[i] = state[h, EXLAG0 + i]
        sndpt = state[h, SNDPT]
        sntmp = state[h, SNTMP]
        tprev = state[h, TPREV]
        age = state[h, ALB_AGE]

        for t in range(nt):
            tair = ta[t, h]
            pxi = px[t, h]
            dta = tair - tprev
            ds = 0.1 if sndpt <= 0.0 else 0.1 * we / sndpt
            albedo = a_old + (a_fresh - a_old) * math.exp(-age / a_tau)
            sxfall = 0.0
            sxmelt = 0.0
            sxgslos = 0.0
            sxrfrz = 0.0
            sfall = 0.0
            rain = 0.0
            rain_in = 0.0
            melt = 0.0
            ros_melt = 0.0
            ros = 0.0
            gmro = 0.0
            robg = 0.0
            packro = 0.0
            aesc = 0.0
            path = 0  # 0: snowpack; 1: bare ground, no snow; 2: snow gone this step
            if pxi == 0.0 and we == 0.0:
                path = 1
            else:
                cnhspx = 0.0
                rainm = 0.0
                if pxi != 0.0:
                    fracs_t = min(max(fracs[t, h], 0.0), 1.0)
                    if fracs_t > 0.0:
                        ts = min(tair, 0.0)
                        sfall = pxi * fracs_t * scf
                        if we + liqw < sbws:
                            if sfall >= snof:
                                sbws = we + liqw + 0.75 * sfall
                        else:
                            sbws = sbws + 0.75 * sfall
                            if sfall >= snof and sb > we + liqw:
                                sb = we + liqw
                        we = we + sfall
                        if we + liqw >= 3.0 * sb:
                            accmax = we + liqw
                            aeadj = 0.0
                        cnhspx = -ts * sfall / 160.0
                        if sfall > sfnew:
                            tindex = ts
                        age = age * math.exp(-sfall / a_ref)
                    rain = pxi * (1.0 - fracs_t)
                    rain_in = rain
                    if we == 0.0:
                        path = 1
                    else:
                        rainm = 0.0125 * rain * max(tair, 0.0)
                if path == 0:
                    if we <= gm:
                        gmro = we + liqw
                        melt = 0.0
                        robg = rain
                        rain = 0.0
                        path = 2
                    else:
                        gmwlos = (gm / we) * liqw
                        gmslos = gm
                        pmelt, cnhs, tindex = _melt19(idn[t], alat, tair, mfmax, mfmin, mbase, tindex, tipm, nmf)
                        sw_melt = 0.0
                        if melt_mode == 1:
                            if tair > rad_tmin:
                                sw_melt = srf * (1.0 - albedo) * sw[t, h] * sw_red * rad_mm
                            pmelt = tf * max(tair - mbase, 0.0) + sw_melt
                        if rain > rfmin:
                            esat = _esat_anderson(tair)
                            if ros_mode == 1:
                                e_air = min(ea[t, h], esat)
                                uadj = max(wfun * wind[t, h] * wind_red, uadj_min) * fit / 6.0
                            else:
                                e_air = 0.90 * esat
                                uadj = uadj_c
                            tak = (tair + 273.0) * 0.01
                            qn = sbci * (tak * tak * tak * tak - 55.55)
                            qe = 8.5 * (e_air - 6.11) * uadj
                            qh = 7.5 * 0.000646 * pa[t, h] * uadj * tair
                            melt = qn + qe + qh + rainm + sw_melt
                            if melt < 0.0:
                                melt = 0.0
                            ros = 1.0
                        else:
                            melt = pmelt + rainm
                        aesc, accmax, sb, sbaesc, sbws, aeadj = _aesc19(we + liqw, accmax, sb, sbaesc, sbws, si, adc, aeadj, snof)
                        if aesc != 1.0:
                            melt = melt * aesc
                            cnhs = cnhs * aesc
                            gmwlos = gmwlos * aesc
                            gmslos = gmslos * aesc
                            robg = (1.0 - aesc) * rain
                            rain = rain - robg
                        if cnhs + neghs < 0.0:
                            cnhs = -neghs
                        sxfall += sfall
                        sxmelt += melt
                        sxgslos += gmslos
                        we = we - gmslos
                        liqw = liqw - gmwlos
                        gmro = gmslos + gmwlos
                        if melt > 0.0 and melt >= we:
                            melt = we + liqw
                            path = 2
                        else:
                            if melt > 0.0:
                                we = we - melt
                            water = melt + rain
                            heat = cnhs + cnhspx
                            liqwmx = plwhc * we
                            neghs = neghs + heat
                            if neghs < 0.0:
                                neghs = 0.0
                            if neghs > 0.33 * we:
                                neghs = 0.33 * we
                            if water + liqw >= liqwmx + neghs + plwhc * neghs:
                                excess = water + liqw - liqwmx - neghs - plwhc * neghs
                                liqw = liqwmx + plwhc * neghs
                                we = we + neghs
                                neghs = 0.0
                            elif water >= neghs:
                                liqw = liqw + water - neghs
                                we = we + neghs
                                sxrfrz += neghs
                                neghs = 0.0
                                excess = 0.0
                            else:
                                we = we + water
                                neghs = neghs - water
                                excess = 0.0
                                sxrfrz += water
                            if neghs == 0.0:
                                tindex = 0.0
                            packro, storge = _rout19(idt, excess, we, aesc, storge, nexlag, exlag)
                            packro = packro + gmro
                        if ros == 1.0:
                            ros_melt = melt
            if path == 2:
                tex = 0.0
                for i in range(nexlag):
                    tex += exlag[i]
                packro = gmro + melt + tex + storge + rain
                we = 0.0
                neghs = 0.0
                liqw = 0.0
                tindex = 0.0
                accmax = 0.0
                sb = 0.0
                sbaesc = 0.0
                sbws = 0.0
                storge = 0.0
                aeadj = 0.0
                sndpt = 0.0
                sntmp = 0.0
                for i in range(7):
                    exlag[i] = 0.0
                aesc = 0.0
            elif path == 1:
                robg = pxi
                packro = 0.0
                aesc = 0.0
                rain_in = pxi

            tex = 0.0
            for i in range(nexlag):
                tex += exlag[i]
            if we > 0.0:
                sliq = liqw + tex + storge
                sxfall = max(sxfall - sxmelt, 0.0)
                sndpt, ds, sntmp = _sndepth(we, sliq, sxfall, sxgslos, sxrfrz, tair, dta, idt, sndpt, ds, sntmp)
            else:
                sndpt = 0.0
                sntmp = 0.0
            twe = we + liqw + tex + storge
            cover = aesc
            if twe != 0.0:
                cover, accmax, sb, sbaesc, sbws, aeadj = _aesc19(we + liqw, accmax, sb, sbaesc, sbws, si, adc, aeadj, snof)
            if twe > 0.0:
                age = age + fit * (3.0 if tair > 0.0 else 1.0)
            else:
                age = 0.0
            tprev = tair

            out[0, t, h] = twe
            out[1, t, h] = packro + robg
            out[2, t, h] = melt
            out[3, t, h] = sfall
            out[4, t, h] = rain_in
            out[5, t, h] = cover if twe > 0.0 else 0.0
            out[6, t, h] = neghs
            out[7, t, h] = liqw + tex + storge
            out[8, t, h] = sndpt / 100.0
            out[9, t, h] = albedo if twe > 0.0 else 0.0
            out[10, t, h] = ros_melt
            out[11, t, h] = ros

        state[h, WE] = we
        state[h, NEGHS] = neghs
        state[h, LIQW] = liqw
        state[h, TINDEX] = tindex
        state[h, ACCMAX] = accmax
        state[h, SB] = sb
        state[h, SBAESC] = sbaesc
        state[h, SBWS] = sbws
        state[h, STORGE] = storge
        state[h, AEADJ] = aeadj
        for i in range(7):
            state[h, EXLAG0 + i] = exlag[i]
        state[h, SNDPT] = sndpt
        state[h, SNTMP] = sntmp
        state[h, TPREV] = tprev
        state[h, ALB_AGE] = age


def day_number_from_march21(times) -> np.ndarray:
    """IDN as computed in EXSNOW19 (including its leap-year handling)."""
    julday = np.array([0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334])
    years = np.asarray(times.year)
    months = np.asarray(times.month)
    days = np.asarray(times.day)
    kda = julday[months - 1] + days
    leap_adj = (years % 4 == 0) & (months >= 3)
    kda = kda + leap_adj
    nda = 365 + leap_adj
    i0 = julday[2] + 21
    i1 = julday[months - 1] + days
    return np.where(kda >= i0, i1 - i0, nda - (i0 - i1)).astype(np.int64)


def initial_state(n_hru: int) -> np.ndarray:
    return np.zeros((n_hru, N_STATE), dtype=np.float64)
