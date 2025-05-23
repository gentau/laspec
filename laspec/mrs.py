__all__ = ["MrsSpec", "MrsEpoch", "MrsFits", "MrsSource", "debad", "SOL_kms"]

import glob
import os
import warnings

import matplotlib.pyplot as plt
import numpy as np
from astropy import constants
from astropy.io import fits
from astropy.table import Table
from scipy.signal import medfilt
from scipy.signal.windows import gaussian

from .normalization import normalize_spectrum_general
from .time import datetime2jd, jd2bjd

SOL_kms = constants.c.value / 1000.0

warnings.filterwarnings("ignore")


def debad(
    wave, fluxnorm, nsigma=(4, 8), mfarg=21, gfarg=(51, 9), maskconv=7, maxiter=3
):
    """
    Parameters
    ----------
    wave:
        wavelength
    fluxnorm:
        normalized flux
    nsigma:
        lower & upper sigma levels
    mfarg:
        median filter width / pixel
    gfarg:
        Gaussian filter length & width / pixel
    maskconv:
        mask convolution --> cushion
    maxiter:
        max iteration

    Return
    ------
    fluxnorm
    """
    npix = len(fluxnorm)
    indclip = np.zeros_like(fluxnorm, dtype=bool)
    countiter = 0
    while True:
        # median filter
        fluxmf = medfilt(fluxnorm, mfarg)
        # gaussian filter
        gk = gaussian(5, 0.5)
        gk /= np.sum(gk)
        fluxmf = np.convolve(fluxmf, gk, "same")
        # residuals
        fluxres = fluxnorm - fluxmf
        # gaussian filter --> sigma
        gk = gaussian(*gfarg)
        gk /= np.sum(gk)
        fluxsigma = np.convolve(np.abs(fluxres), gk, "same")
        # clip
        indout = np.logical_or(
            fluxres > fluxsigma * nsigma[0], fluxres < -fluxsigma * nsigma[1]
        )
        indclip |= indout
        if np.sum(indclip) > 0.5 * npix:
            raise RuntimeError("Too many bad pixels!")
        countiter += 1
        if np.sum(indout) == 0 or countiter >= maxiter:
            return fluxnorm
        else:
            indout = np.convolve(indout * 1.0, np.ones((maskconv,)), "same") > 0
            fluxnorm = np.interp(wave, wave[~indout], fluxnorm[~indout])


class MrsSpec:
    """MRS spectrum"""

    name = ""
    # original quantities
    wave = np.array([], dtype=float)
    flux = np.array([], dtype=float)
    ivar = np.array([], dtype=float)
    mask = np.array([], dtype=bool)  # True for problematic
    flux_err = np.array([], dtype=float)
    indcr = np.array([], dtype=float)  # cosmic ray index

    # normalized quantities
    flux_norm = np.array([], dtype=float)
    flux_cont = np.array([], dtype=float)
    ivar_norm = np.array([], dtype=float)
    flux_norm_err = np.array([], dtype=float)

    # other information (optional)
    info = {}
    rv = 0.0

    # time and position info
    filename = ""
    snr = 0
    exptime = 0
    lmjm = 0
    lmjmlist = []
    obsid = 0
    seeing = 0.0
    lamplist = ""
    ra = 0.0
    dec = 0.0
    fibertype = ""
    fibermask = 0.0
    jdbeg = 0
    jdend = 0
    jdmid = 0
    jdltt = 0.0
    bjdmid = 0.0

    # status
    isempty = True
    isnormalized = False

    # default settings for normalize_spectrum_iter / normlize_spectrum_poly
    norm_type = None
    norm_kwargs = {}

    def meta(self):
        return dict(
            extname=self.extname,
            snr=self.snr,
            exptime=self.exptime,
            lmjm=self.lmjm,
            lamplist=None,
            # time and position info
            filename="",
            obsid=0,
            seeing=0,
            ra=0.0,
            dec=0.0,
            fibertype=self.fibertype,
            fibermask=self.fibermask,
            jdbeg=self.jdbeg,
            jdend=self.jdend,
            jdmid=self.jdmid,
            jdltt=self.jdltt,
            bjdmid=self.bjdmid,
        )

    def __init__(
        self,
        wave=None,
        flux=None,
        ivar=None,
        mask=None,
        info={},
        norm_type="spline",
        **norm_kwargs,
    ):
        """a general form of spectrum
        Parameters
        ----------
        wave:
            array, wavelength
        flux:
            array, flux
        ivar:
            array, ivar
        mask:
            array(int), 1 for problematic
        info:
            dict, information of this spectrum
        norm_type:
            poly/spline/None, if set, normalize spectrum after initialization
        norm_kwargs:
            normalization settings passed to normalize_spectrum_general()
        """
        # set data
        if wave is not None and flux is not None:
            self.wave, self.flux = wave, flux
            self.isempty = False
        else:
            # a null spec
            self.wave = np.array([], dtype=float)
            self.flux = np.array([], dtype=float)
            self.isempty = True
        # ivar and mask is optional for spec
        if ivar is None:
            self.ivar = np.ones_like(self.flux, dtype=float)
        else:
            self.ivar = ivar
        if mask is None:
            self.mask = np.zeros_like(self.flux, dtype=bool)
            self.npix_bad = 0
        else:
            self.mask = mask
            self.npix_bad = np.sum(self.mask > 0)
        # flux_err
        flux_err = self.ivar**-0.5
        self.flux_err = np.where(np.isfinite(flux_err), flux_err, np.nan)
        # set info
        for k, v in info.items():
            self.__setattr__(k, v)
        self.info = info

        # normalize spectrum
        self.norm_type = norm_type
        self.norm_kwargs = norm_kwargs
        if norm_type in ["poly", "spline"]:
            # normalize
            self.normalize(norm_type=norm_type, **norm_kwargs)
            self.isnormalized = True
        return

    def __repr__(self):
        return "<MrsSpec name={} snr={:.1f}>".format(self.name, self.snr)

    @staticmethod
    def from_hdu(hdu=None, norm_type=None, **norm_kwargs):
        """convert MRS HDU to spec"""
        if hdu is None or hdu.header["EXTNAME"] == "Information":
            return MrsSpec()
        else:
            # convert to Table
            spec = Table(hdu.data)
            if "LOGLAM" in spec.colnames:
                # this is old format, until DR9 v0
                spec.sort("LOGLAM")
                if "COADD" in hdu.name:
                    # it's coadded spec
                    wave = 10 ** spec["LOGLAM"].data
                    flux = spec["FLUX"].data
                    ivar = spec["IVAR"].data
                    mask = spec["ORMASK"].data  # use ormask for coadded spec
                elif hdu.name.startswith("B-") or hdu.name.startswith("R-"):
                    # it's epoch spec
                    wave = 10 ** spec["LOGLAM"].data
                    flux = spec["FLUX"].data
                    ivar = spec["IVAR"].data
                    mask = spec["PIXMASK"].data  # use pixmask for epoch spec
            elif "WAVELENGTH" in spec.colnames:
                # new format, since DR9 v1
                if "COADD" in hdu.name:
                    # it's coadded spec
                    wave = spec["WAVELENGTH"][0]
                    flux = spec["FLUX"][0]
                    ivar = spec["IVAR"][0]
                    mask = spec["ORMASK"][0]  # use ormask for coadded spec
                elif hdu.name.startswith("B-") or hdu.name.startswith("R-"):
                    # it's epoch spec
                    wave = spec["WAVELENGTH"][0]
                    flux = spec["FLUX"][0]
                    ivar = spec["IVAR"][0]
                    mask = spec["PIXMASK"][0]  # use pixmask for epoch spec
            else:
                raise ValueError("@MrsFits: error in reading epoch spec!")

            # get meta info
            info = dict(
                name=get_kwd_safe(hdu.header, "EXTNAME", ""),
                lmjm=int(get_kwd_safe(hdu.header, "LMJM", 0)),
                exptime=float(get_kwd_safe(hdu.header, "EXPTIME", 0.0)),
                snr=float(get_kwd_safe(hdu.header, "SNR", 0.0)),
                lamplist=get_kwd_safe(hdu.header, "LAMPLIST", ""),
            )

            # initiate MrsSpec
            ms = MrsSpec(
                wave, flux, ivar, mask, info=info, norm_type=norm_type, **norm_kwargs
            )

            # calculate bjdmid
            ms.jdbeg = datetime2jd(
                hdu.header["DATE-BEG"], format="isot", tz_correction=8
            )
            ms.jdend = datetime2jd(
                hdu.header["DATE-END"], format="isot", tz_correction=8
            )
            ms.jdmid = (ms.jdbeg + ms.jdend) / 2.0
            ms.bjdmid = jd2bjd(ms.ra, ms.dec, ms.jdmid)
            return ms

    @staticmethod
    def from_mrs(fp_mrs, hduname="COADD_B", norm_type=None, **norm_kwargs):
        """read from MRS fits file"""
        hl = fits.open(fp_mrs)
        ms = MrsSpec.from_hdu(hl[hduname], norm_type=norm_type, **norm_kwargs)
        return ms

    @staticmethod
    def from_lrs(fp_lrs, norm_type="spline", **norm_kwargs):
        """read from LRS fits file"""
        hl = fits.open(fp_lrs)
        hdr = hl[0].header
        try:
            flux, ivar, wave, andmask, ormask = hl[0].data
        except:
            # dr9
            flux = hl[1].data["FLUX"][0]
            ivar = hl[1].data["IVAR"][0]
            wave = hl[1].data["WAVELENGTH"][0]
            # andmask = hl[1].data["ANDMASK"][0]
            ormask = hl[1].data["ORMASK"][0]

        # info
        info = dict(
            name=hdr["OBSID"],
            obsid=hdr["OBSID"],
            ra=hdr["RA"],
            dec=hdr["DEC"],
            rv=hdr["Z"] * SOL_kms,
            rv_err=hdr["Z_ERR"] * SOL_kms,
            subclass=hdr["SUBCLASS"],
            tsource=hdr["TSOURCE"],
            snr=hdr["SNRG"],
            snru=hdr["SNRU"],
            snrg=hdr["SNRG"],
            snrr=hdr["SNRR"],
            snri=hdr["SNRI"],
            snrz=hdr["SNRZ"],
        )
        ms = MrsSpec(
            wave, flux, ivar, ormask, info=info, norm_type=norm_type, **norm_kwargs
        )
        # calculate bjd
        if hdr["DATE-BEG"] != "" and hdr["DATE-END"] != "":
            ms.jdbeg = datetime2jd(hdr["DATE-BEG"], format="isot", tz_correction=8)
            ms.jdend = datetime2jd(hdr["DATE-END"], format="isot", tz_correction=8)
            ms.jdmid = (ms.jdbeg + ms.jdend) / 2.0
        elif hdr["DATE-OBS"] != "":
            ms.jdmid = datetime2jd(hdr["DATE-OBS"], format="isot", tz_correction=8)
        ms.bjdmid = jd2bjd(ms.ra, ms.dec, ms.jdmid)
        return ms

    def normalize(self, llim=0.0, norm_type=None, **norm_kwargs):
        """normalize spectrum with (optional) new settings"""
        if not self.isempty:
            # for normal spec
            if norm_type is not None:
                self.norm_type = norm_type
                self.norm_kwargs.update(norm_kwargs)
                # normalize spectrum
                self.flux_norm, self.flux_cont = normalize_spectrum_general(
                    self.wave,
                    np.where(self.flux < llim, llim, self.flux),
                    self.norm_type,
                    **self.norm_kwargs,
                )
                self.ivar_norm = self.ivar * self.flux_cont**2
                self.flux_norm_err = self.flux_err / self.flux_cont
            else:
                self.norm_type = norm_type
                self.norm_kwargs.update(norm_kwargs)
                # normalize spectrum
                self.flux_norm = np.array([], dtype=float)
                self.flux_cont = np.array([], dtype=float)
                self.ivar_norm = np.array([], dtype=float)
                self.flux_norm_err = np.array([], dtype=float)

        else:
            # for empty spec
            # update norm kwargs
            self.norm_kwargs.update(norm_kwargs)
            # normalize spectrum
            self.flux_norm = np.array([], dtype=float)
            self.flux_cont = np.array([], dtype=float)
            self.ivar_norm = np.array([], dtype=float)
            self.flux_norm_err = np.array([], dtype=float)
            return

    def wave_rv(self, rv=None):
        """calculate RV-corrected wavelength array

        Parameters
        ----------
        rv: float
            radial velocity in km/s

        """
        if rv is None:
            rv = self.rv
        return self.wave / (1 + rv / SOL_kms)

    def interp(self, new_wave, rv=None):
        """interpolate to a new wavelength grid"""
        return np.interp(new_wave, self.wave_rv(rv), self.flux)

    def interp_then_norm(self, new_wave, rv=None):
        """interpolate to a new wavelength grid"""
        flux_interp = np.interp(new_wave, self.wave_rv(rv), self.flux)
        flux_norm, flux_cont = normalize_spectrum_general(
            new_wave, flux_interp, norm_type=self.norm_type, **self.norm_kwargs
        )
        flux_norm_err = np.interp(new_wave, self.wave_rv(rv), self.flux_err) / flux_cont
        return flux_norm, flux_norm_err

    def interp_norm(self, new_wave, rv=None):
        """interpolate to a new wavelength grid"""
        return np.interp(new_wave, self.wave_rv(rv), self.flux_norm)

    def plot(self):
        plt.plot(self.wave, self.flux)

    def plot_norm(self, shift=0):
        plt.plot(self.wave, self.flux_norm + shift)

    def plot_cont(self):
        plt.plot(self.wave, self.flux_cont)

    def plot_err(self):
        plt.plot(self.wave, self.flux_err)

    def plot_norm_err(self, shift=0):
        plt.plot(self.wave, self.flux_norm_err + shift)

    def reduce(
        self,
        wave_new=None,
        rv=0,
        npix_cushion=50,
        cr=True,
        nsigma=(4, 8),
        maxiter=5,
        norm_type="spline",
        niter=2,
        flux_bounds=(0, 3),
    ):
        """

        Parameters
        ----------
        wave_new:
            if specified, spectrum is interpolated to wave_new
        rv:
            if specified, radial velocity is corrected
        npix_cushion: int
            if speficied, cut the two ends
        cr:
            if True, remove cosmic rays using the *debad* function
        nsigma:
            sigma levels used in removing cosmic rays
        maxiter:
            max number of iterations used in removing cosmic rays
        norm_type:
            "spline" | None
        niter:
            number iterations in normalization

        Returns
        -------
        wave_new, flux_norm, flux_norm_err
        """
        # determine the chunk range
        if npix_cushion > 0:
            npix0 = npix_cushion
            npix1 = -npix_cushion
        else:
            npix0 = 0
            npix1 = len(self.wave)

        # cut spectrum
        wave_obs = self.wave[npix0:npix1]
        flux_err = self.flux_err[npix0:npix1]
        mask = self.mask[npix0:npix1] > 0  # positive for bad pixels
        # remove cosmic rays if cr is True, also fill the negative pixels with 0.
        if cr:
            flux_obs = np.where(self.flux > 0, self.flux, 0.0)
            flux_obs = debad(self.wave, flux_obs, nsigma=nsigma, maxiter=maxiter)[
                npix0:npix1
            ]
            indcr = (np.abs(flux_obs - self.flux[npix0:npix1]) > 1e-5) * 1
        else:
            flux_obs = self.flux[npix0:npix1]
            flux_obs = np.where(flux_obs > 0, flux_obs, 0.0)
            indcr = np.zeros(flux_obs.shape, dtype=int)

        # use new wavelength grid if wave_new is specified
        wave_obsz0 = wave_obs / (1 + rv / SOL_kms)
        if wave_new is None:
            wave_new = wave_obsz0
        elif len(wave_new) == 2:
            # this indicates the new wavelength limits
            wave_new = wave_obsz0[
                (wave_obsz0 > wave_new[0]) & (wave_obsz0 < wave_new[1])
            ]
        # else: wave_new is specified

        flux_obs = np.interp(wave_new, wave_obsz0, flux_obs)
        flux_err = np.interp(wave_new, wave_obsz0, flux_err)
        mask = 1 * (np.interp(wave_new, wave_obsz0, mask | indcr) > 0)

        msr = MrsSpec()
        msr.wave = wave_new
        msr.flux = flux_obs
        msr.ivar = flux_err**-2
        msr.flux_err = flux_err
        msr.mask = mask
        msr.npix_bad = self.npix_bad
        msr.indcr = indcr
        msr.name = self.name
        msr.isempty = self.isempty

        # other information (optional)
        msr.info = self.info
        msr.rv = self.rv

        # time and position info
        msr.filename = self.filename
        msr.snr = self.snr
        msr.exptime = self.exptime
        msr.lmjm = self.lmjm
        msr.lmjmlist = self.lmjmlist
        msr.obsid = self.obsid
        msr.seeing = self.seeing
        msr.lamplist = self.lamplist
        msr.ra = self.ra
        msr.dec = self.dec
        msr.fibertype = self.fibertype
        msr.fibermask = self.fibermask
        msr.jdbeg = self.jdbeg
        msr.jdend = self.jdend
        msr.jdmid = self.jdmid
        msr.jdltt = self.jdltt
        msr.bjdmid = self.bjdmid

        # normalize spectrum if norm_type is specified
        if norm_type is not None:
            msr.normalize(norm_type=norm_type, niter=niter)

        return msr


class MrsEpoch:
    """MRS epoch spcetrum"""

    nspec = 0
    speclist = []
    specnames = []
    # the most important attributes
    epoch = -1
    lmjm = 0
    snr = []
    rv = 0.0

    # time and position info
    filename = ""
    obsid = 0
    seeing = 0.0
    ra = 0.0
    dec = 0.0
    fibertype = ""
    fibermask = 0.0
    jdbeg = 0.0
    jdend = 0.0
    jdmid = 0.0
    jdltt = 0.0
    jdmid_delta = 0.0
    bjdmid = 0.0

    wave = np.array([], dtype=float)
    flux = np.array([], dtype=float)
    ivar = np.array([], dtype=float)
    mask = np.array([], dtype=int)
    flux_err = np.array([], dtype=float)

    flux_norm = np.array([], dtype=float)
    ivar_norm = np.array([], dtype=float)
    flux_cont = np.array([], dtype=float)
    flux_norm_err = np.array([], dtype=float)

    # # default settings for normalize_spectrum_iter/poly
    norm_kwargs = {}

    def __init__(
        self, speclist, specnames=("B", "R"), epoch=-1, norm_type=None, **norm_kwargs
    ):
        """combine B & R to an epoch spectrum
        In this list form, it is compatible with even echelle spectra

        speclist:
            spectrum list
        specnames:
            the names of spectra, will be used as suffix
        epoch:
            the epoch of this epoch spectrum
        norm_type:
            if True, normalize spectra in initialization
        norm_kwargs:
            the normalization settings
        """
        # set epoch
        self.epoch = epoch

        # update norm kwargs
        self.norm_kwargs.update(norm_kwargs)

        self.nspec = len(speclist)
        # default name is spec order
        if specnames is None or len(specnames) == 0:
            specnames = [i for i in range(self.nspec)]
        self.speclist = speclist
        self.specnames = specnames

        # store spectrum data
        self.snr = [spec.snr for spec in self.speclist]
        # normalize spectra
        self.normalize(llim=0.0, norm_type=norm_type, **norm_kwargs)

        return

    def __repr__(self):
        s = "[MrsEpoch epoch={} nspec={}]".format(self.epoch, self.nspec)
        for i in range(self.nspec):
            s += "\n{}".format(self.speclist[i])
        return s

    def normalize(self, llim=0.0, norm_type=None, **norm_kwargs):
        """normalize each spectrum with (optional) new settings"""
        # update norm kwargs
        self.norm_kwargs.update(norm_kwargs)

        # normalize each spectrum
        for i_spec in range(self.nspec):
            self.speclist[i_spec].normalize(
                llim=llim, norm_type=norm_type, **self.norm_kwargs
            )

            # store each spec
            self.__setattr__(
                "wave_{}".format(self.specnames[i_spec]), self.speclist[i_spec].wave
            )
            self.__setattr__(
                "flux_{}".format(self.specnames[i_spec]), self.speclist[i_spec].flux
            )
            self.__setattr__(
                "ivar_{}".format(self.specnames[i_spec]), self.speclist[i_spec].ivar
            )
            self.__setattr__(
                "mask_{}".format(self.specnames[i_spec]), self.speclist[i_spec].mask
            )
            self.__setattr__(
                "flux_err_{}".format(self.specnames[i_spec]),
                self.speclist[i_spec].flux_err,
            )

            self.__setattr__(
                "flux_norm_{}".format(self.specnames[i_spec]),
                self.speclist[i_spec].flux_norm,
            )
            self.__setattr__(
                "ivar_norm_{}".format(self.specnames[i_spec]),
                self.speclist[i_spec].ivar_norm,
            )
            self.__setattr__(
                "flux_cont_{}".format(self.specnames[i_spec]),
                self.speclist[i_spec].flux_cont,
            )
            self.__setattr__(
                "flux_norm_err_{}".format(self.specnames[i_spec]),
                self.speclist[i_spec].flux_norm_err,
            )

        # combined attributes
        self.wave = np.array([], dtype=float)
        self.flux = np.array([], dtype=float)
        self.ivar = np.array([], dtype=float)
        self.mask = np.array([], dtype=int)
        self.flux_err = np.array([], dtype=float)

        self.flux_norm = np.array([], dtype=float)
        self.ivar_norm = np.array([], dtype=float)
        self.flux_cont = np.array([], dtype=float)
        self.flux_norm_err = np.array([], dtype=float)

        # concatenate into one epoch spec
        for i_spec in range(self.nspec):
            if not self.speclist[i_spec].isempty:
                self.wave = np.append(self.wave, self.speclist[i_spec].wave)
                self.flux = np.append(self.flux, self.speclist[i_spec].flux)
                self.ivar = np.append(self.ivar, self.speclist[i_spec].ivar)
                self.mask = np.append(self.mask, self.speclist[i_spec].mask)
                self.flux_err = np.append(self.flux_err, self.speclist[i_spec].flux_err)

                self.flux_norm = np.append(
                    self.flux_norm, self.speclist[i_spec].flux_norm
                )
                self.ivar_norm = np.append(
                    self.ivar_norm, self.speclist[i_spec].ivar_norm
                )
                self.flux_cont = np.append(
                    self.flux_cont, self.speclist[i_spec].flux_cont
                )
                self.flux_norm_err = np.append(
                    self.flux_norm_err, self.speclist[i_spec].flux_norm_err
                )
        return

    def wave_rv(self, rv=None):
        """
        calculate RV-corrected wavelength array

        Parameters
        ----------
        rv: float
            radial velocity in km/s

        """
        if rv is None:
            rv = self.rv
        return self.wave / (1 + rv / SOL_kms)

    def flux_norm_dbd(self, **kwargs):
        """return fixed flux_norm"""
        return debad(self.wave, self.flux_norm, *kwargs)

    def plot(self):
        plt.plot(self.wave, self.flux)

    def plot_norm(self, shift=0):
        plt.plot(self.wave, self.flux_norm + shift)

    def plot_cont(self):
        plt.plot(self.wave, self.flux_cont)

    def plot_err(self):
        plt.plot(self.wave, self.flux_err)

    def plot_norm_err(self, shift=0):
        plt.plot(self.wave, self.flux_norm_err + shift)

    def plot_reduce(self):
        for i in range(self.nspec):
            msr = self.speclist[i].reduce(norm_type=None)
            msr.plot()

    def plot_norm_reduce(self, shift=0):
        for i in range(self.nspec):
            msr = self.speclist[i].reduce(norm_type="spline")
            msr.plot_norm(shift=shift)

    def reduce(self, wave_new_list=None, norm_type="spline", niter=3, **rdc_kwargs):
        """

        Parameters
        ----------
        wave_new_list:
            new wavelength grid list that will be interpolated to
        norm_type:
            type of normalization
        niter:
            number of iteration in normalization

        Returns
        -------
        mer: MrsEpoch
            reduced epoch spectrum

        """
        if wave_new_list is None:
            mer = MrsEpoch(
                [self.speclist[i].reduce(**rdc_kwargs) for i in range(self.nspec)],
                specnames=self.specnames,
                norm_type=norm_type,
                niter=niter,
            )
        else:
            assert len(wave_new_list) == self.nspec
            mer = MrsEpoch(
                [
                    self.speclist[i].reduce(wave_new=wave_new_list[i], **rdc_kwargs)
                    for i in range(self.nspec)
                ],
                specnames=self.specnames,
                norm_type=norm_type,
                niter=niter,
            )
        # header info
        mer.epoch = self.epoch
        mer.lmjm = self.lmjm
        mer.snr = self.snr
        mer.rv = self.rv
        # time and position info
        mer.filename = self.filename
        mer.obsid = self.obsid
        mer.seeing = self.seeing
        mer.ra = self.ra
        mer.dec = self.dec
        mer.fibertype = self.fibertype
        mer.fibermask = self.fibermask
        mer.jdbeg = self.jdbeg
        mer.jdend = self.jdend
        mer.jdmid = self.jdmid
        mer.jdltt = self.jdltt
        mer.jdmid_delta = self.jdmid_delta
        mer.bjdmid = self.bjdmid
        return mer

    @property
    def exptime(self):
        return np.array([_.exptime for _ in self.speclist])


class MrsFits(fits.HDUList):
    nhdu = 0
    hdunames = []
    isB = []
    isR = []
    isCoadd = []
    isEpoch = []
    ulmjm = []

    def __init__(self, fp=None):
        """set file path and read data"""
        # check fits existence
        if fp is None:
            print("@MrsSpec: file path is not set!")
        elif not os.path.exists(fp):
            raise RuntimeError("@MrsSpec: file not found! ", fp)
        else:
            self.filepath = fp
        # read HDU list
        super().__init__(fits.open(fp))
        # get HDU names
        self.nhdu = len(self)
        self.hdunames = [hdu.name for hdu in self]
        self.ulmjm = []

        self.isB = np.zeros(self.nhdu, dtype=bool)
        self.isR = np.zeros(self.nhdu, dtype=bool)
        self.isEpoch = np.zeros(self.nhdu, dtype=bool)
        self.isCoadd = np.zeros(self.nhdu, dtype=bool)
        self.lmjm = np.zeros(self.nhdu, dtype=int)
        for i in range(self.nhdu):
            if self.hdunames[i].startswith("B-"):
                self.isB[i] = True
                self.isEpoch[i] = True
                self.lmjm[i] = int(self.hdunames[i][2:])
            elif self.hdunames[i].startswith("R-"):
                self.isR[i] = True
                self.isEpoch[i] = True
                self.lmjm[i] = int(self.hdunames[i][2:])
            elif self.hdunames[i] == "COADD_B":
                self.isB[i] = True
                self.isCoadd[i] = True
            elif self.hdunames[i] == "COADD_R":
                self.isR[i] = True
                self.isCoadd[i] = True
            elif not self.hdunames[i] == "Information":
                raise RuntimeError("@MrsSpec: error during processing HDU name")

    def __repr__(self):
        """as self.info()
        Summarize the info of the HDUs in this `HDUList`.
        Note that this function prints its results to the console---it
        does not return a value.
        """
        if self._file is None:
            name = "(No file associated with this HDUList)"
        else:
            name = self._file.name
        results = [
            f"Filename: {name}",
            "No.    Name      Ver    Type      Cards   Dimensions   Format",
        ]
        format = "{:3d}  {:10}  {:3} {:11}  {:5d}   {}   {}   {}"
        default = ("", "", "", 0, (), "", "")
        for idx, hdu in enumerate(self):
            summary = hdu._summary()
            if len(summary) < len(default):
                summary += default[len(summary) :]
            summary = (idx,) + summary
            results.append(format.format(*summary))
        return "\n".join(results[1:])

    # or get one spec (specify a key)?
    def get_one_spec(self, lmjm="COADD", band="B"):
        if lmjm == "COADD":
            k = "COADD_{}".format(band.strip())
        else:
            k = "{}-{}".format(band.strip(), lmjm)
        return MrsSpec.from_hdu(self[k])

    def get_one_epoch(self, lmjm=84420148, norm_type="spline", **norm_kwargs):
        """get one epoch spec from fits"""
        try:
            if isinstance(lmjm, str):
                assert lmjm == "COADD"
            if isinstance(lmjm, int):
                assert lmjm in self.lmjm
        except AssertionError:
            raise AssertionError(
                "@MrsFits: lmjm={} is not found in this file!".format(lmjm)
            )

        if lmjm == "COADD":
            kB = "COADD_B"
            kR = "COADD_R"
        else:
            kB = "B-{}".format(lmjm)
            kR = "R-{}".format(lmjm)
        # read B & R band spec
        if kB in self.hdunames:
            msB = MrsSpec.from_hdu(self[kB], norm_type=norm_type, **norm_kwargs)
        else:
            msB = MrsSpec(norm_type=norm_type, **norm_kwargs)
        if kR in self.hdunames:
            msR = MrsSpec.from_hdu(self[kR], norm_type=norm_type, **norm_kwargs)
        else:
            msR = MrsSpec(norm_type=norm_type, **norm_kwargs)
        # set epoch info
        me = MrsEpoch(
            (msB, msR),
            specnames=("B", "R"),
            epoch=lmjm,
            norm_type=norm_type,
            **norm_kwargs,
        )
        # set additional infomation
        me.filename = self[0].header["FILENAME"]
        me.obsid = self[0].header["OBSID"]
        me.seeing = self[0].header["SEEING"]
        me.ra = self[0].header["RA"]
        me.dec = self[0].header["DEC"]
        me.fibertype = (
            self[0].header["FIBERTYP"] if "FIBERTYP" in self[0].header.keys() else None
        )
        me.fibermask = (
            self[0].header["FIBERMAS"] if "FIBERMAS" in self[0].header.keys() else None
        )

        try:
            if kB in self.hdunames and kR not in self.hdunames:
                me.jdbeg = datetime2jd(
                    self[kB].header["DATE-BEG"], format="isot", tz_correction=8
                )
                me.jdend = datetime2jd(
                    self[kB].header["DATE-END"], format="isot", tz_correction=8
                )
                me.jdmid = (me.jdbeg + me.jdend) / 2.0
                me.bjdmid = jd2bjd(me.ra, me.dec, me.jdmid)
            elif kB not in self.hdunames and kR in self.hdunames:
                me.jdbeg = datetime2jd(
                    self[kR].header["DATE-BEG"], format="isot", tz_correction=8
                )
                me.jdend = datetime2jd(
                    self[kR].header["DATE-END"], format="isot", tz_correction=8
                )
                me.jdmid = (me.jdbeg + me.jdend) / 2.0
                me.bjdmid = jd2bjd(me.ra, me.dec, me.jdmid)
            elif kB in self.hdunames and kR in self.hdunames:
                # both records
                jdbeg_B = datetime2jd(
                    self[kB].header["DATE-BEG"], format="isot", tz_correction=8
                )
                jdend_B = datetime2jd(
                    self[kB].header["DATE-END"], format="isot", tz_correction=8
                )
                jdbeg_R = datetime2jd(
                    self[kR].header["DATE-BEG"], format="isot", tz_correction=8
                )
                jdend_R = datetime2jd(
                    self[kR].header["DATE-END"], format="isot", tz_correction=8
                )
                jdmid_B = (jdbeg_B + jdend_B) / 2.0
                jdmid_R = (jdbeg_R + jdend_R) / 2.0
                jdmid_delta = jdmid_B - jdmid_R
                me.jdbeg = jdbeg_B
                me.jdend = jdend_B
                me.jdmid = jdmid_B
                me.bjdmid = jd2bjd(me.ra, me.dec, me.jdmid)
        except Exception as ex:
            print("Keywords DATE-* not found from file {}!".format(me.filename))

        return me

    def get_all_epochs(self, including_coadd=False, norm_type=None, **norm_kwargs):
        # make keys
        if not including_coadd:
            all_keys = []
        else:
            all_keys = [
                "COADD",
            ]
        all_keys.extend(np.unique(self.lmjm[self.lmjm > 0]))
        # return epochs
        return [
            self.get_one_epoch(k, norm_type=norm_type, **norm_kwargs) for k in all_keys
        ]

    @property
    def ls_epoch(self):
        return np.unique(self.lmjm[self.lmjm > 0])

    @property
    def ls_snr(self):
        return np.unique(self.snr[self.lmjm > 0])

    @property
    def epoch(self):
        return self.lmjm

    @property
    def snr(self):
        _snr = np.zeros((self.nhdu,), dtype=float)
        for i in range(self.nhdu):
            if "SNR" in self[i].header.keys():
                _snr[i] = self[i].header["SNR"]
        return _snr


class MrsSource(np.ndarray):
    """array of MrsEpoch instances,"""

    mes = []  # MrsEpoch list #
    name = ""  # source name

    @property
    def snr(self):
        return np.array([_.snr for _ in self], dtype=float)

    @property
    def epoch(self):
        return np.array([_.epoch for _ in self], dtype=float)

    @property
    def nepoch(self):
        return len(self)

    @property
    def rv(self):
        return np.array([_.rv for _ in self], dtype=float)

    def __new__(cls, data, name="", norm_type=None, **norm_kwargs):
        # prepare
        # print(data)
        data = np.array(data, dtype=MrsEpoch)
        # sort
        indsort = np.argsort([_.epoch for _ in data])
        data = data[indsort]
        # substantiate
        msrc = super(MrsSource, cls).__new__(
            cls, buffer=data, dtype=data.dtype, shape=data.shape
        )
        # normalize if necessary
        msrc.normalize(norm_type=norm_type, **norm_kwargs)
        return msrc

    # def __repr__(self):
    #     s = "{{MrsSource nepoch={} name={}}}\n".format(self.nepoch, self.name)
    #     for i in range(self.nepoch):
    #         s += self.mes[i].__repr__().split("\n")[1]
    #         s += "\n"
    #     return s

    @staticmethod
    def glob(fmt, norm_type=None, **norm_kwargs):
        fps = glob.glob(fmt)
        fps.sort()
        return MrsSource.read(fps, norm_type=norm_type, **norm_kwargs)

    @staticmethod
    def read(fps, norm_type=None, **norm_kwargs):
        mes = []
        for fp in fps:
            mf = MrsFits(fp)
            mes.extend(mf.get_all_epochs(norm_type=norm_type, **norm_kwargs))
        return MrsSource(mes, norm_type=norm_type, **norm_kwargs)

    def normalize(self, norm_type=None, **norm_kwargs):
        # normalization
        for i in range(self.nepoch):
            self[i].normalize(norm_type=norm_type, **norm_kwargs)
        return

    @property
    def jdmid(self):
        return np.array([_.__getattribute__("jdmid") for _ in self])

    @property
    def bjdmid(self):
        return np.array([_.__getattribute__("bjdmid") for _ in self])

    @property
    def jdltt(self):
        return np.array([_.__getattribute__("jdltt") for _ in self])

    def getkwd(self, k):
        return np.array([_.__getattribute__(k) for _ in self])

    def shiftplot(self, shift=1.0):
        fig = plt.figure()
        for i, me in enumerate(self):
            plt.plot(me.wave, me.flux_norm + i * shift)
        return fig


def get_kwd_safe(hdr, key, fallback=0.0):
    try:
        return hdr[key]
    except Exception:
        return fallback
