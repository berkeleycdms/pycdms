import numpy as np
from scipy.optimize import least_squares
import numpy as np
from numpy.fft import rfft, fft, ifft, fftfreq, rfftfreq
from qetpy.plotting import plotnonlin, plotnSmBOFFit
import itertools
import matplotlib.pyplot as plt
from time import time
import timeit
from matplotlib.patches import Ellipse


__all__ = ["OptimumFilter","ofamp", "ofamp_pileup", "ofamp_pileup_stationary", "of_nSmB_setup", "of_nSmB_inside", "of_nSmB_fftTemplate", "get_slope_dc_template_nsmb", "chi2lowfreq","chi2_nopulse", "OFnonlin", "MuonTailFit"]

def _argmin_chi2(chi2, nconstrain=None, lgcoutsidewindow=False, constraint_mask=None):
    """
    Helper function for finding the index for the minimum of a chi^2. Includes
    options for constraining the values of chi^2.
    
    Parameters
    ----------
    chi2 : ndarray
        An array containing the chi^2 to minimize. If `chi2` has dimension greater
        than 1, then it is minimized along the last axis.
    nconstrain : NoneType, int, optional
        This is the length of the window (in bins) out of which to constrain the 
        possible values to in the chi^2 minimization, centered on the middle value 
        of `chi2`. Default is None, where `chi2` is uncontrained.
    lgcoutsidewindow : bool, optional
        If False, then the function will minimize the chi^2 in the bins inside the 
        constrained window specified by `nconstrain`, which is the default behavior.
        If True, the function will minimize the chi^2 in the bins outside the range 
        specified by `nconstrain`.
    constraint_mask : NoneType, boolean ndarray, optional
        An additional constraint on the chi^2 to apply, which should be in the form 
        of a boolean mask. If left as None, no additional constraint is applied.
    
    Returns
    -------
    bestind : int, ndarray, float
        The index of the minimum of `chi2` given the constraints specified by 
        `nconstrain` and `lgcoutsidewindow`. If the dimension of `chi2` is greater
        than 1, then this will be an ndarray of ints.
    
    """
    
    nbins = chi2.shape[-1]
    
    if nconstrain is not None:
        if nconstrain>nbins:
            nconstrain = nbins
        elif nconstrain <= 0:
            raise ValueError(f"nconstrain must be a positive integer less than {nbins}")
        
        if lgcoutsidewindow:
            inds = np.r_[0:nbins//2-nconstrain//2, -nbins//2+nconstrain//2+nconstrain%2:0]
            inds[inds<0]+=nbins
        else:
            inds = np.arange(nbins//2-nconstrain//2, nbins//2+nconstrain//2+nconstrain%2)
            
        if constraint_mask is not None:
            inds = inds[constraint_mask[inds]]
        if len(inds)!=0:
            bestind = np.argmin(chi2[..., inds], axis=-1)
            bestind = inds[bestind]
        else:
            bestind = np.nan
    else:
        if constraint_mask is None:
            bestind = np.argmin(chi2, axis=-1)
        else:
            inds = np.flatnonzero(constraint_mask)
            if len(inds)!=0:
                bestind = np.argmin(chi2[..., constraint_mask], axis=-1)
                bestind = inds[bestind]
            else:
                bestind = np.nan
    
    return bestind

def _get_pulse_direction_constraint_mask(amps, pulse_direction_constraint=0):
    """
    Helper function for returning the constraint mask for positive or negative only
    going pulses.
    
    Parameters
    ----------
    amps : ndarray
        Array of the OF amplitudes to use when getting the constraint_mask.
    pulse_direction_constraint : int, optional
        Sets a constraint on the direction of the fitted pulse. If 0, then no constraint on the 
        pulse direction is set. If 1, then a positive pulse constraint is set for all fits. 
        If -1, then a negative pulse constraint is set for all fits. If any other value, then
        an ValueError will be raised.
        
    Returns
    -------
    constraint_mask : NoneType, ndarray
        If not constraint is set, this is set to None. If `pulse_direction_constraint` is 1 or -1, 
        then this is the boolean array of the constraint.
        
    """
    
    if pulse_direction_constraint==0:
        constraint_mask = None
    elif pulse_direction_constraint==1:
        constraint_mask = amps > 0
    elif pulse_direction_constraint==-1:
        constraint_mask = amps < 0
    else:
        raise ValueError("pulse_direction_constraint should be set to 0, 1, or -1")
    
    return constraint_mask

class OptimumFilter(object):
    """
    Class for efficient calculation of the various different Optimum Filters. Written to minimize the
    amount of repeated computations when running multiple on the same data.
    
    Attributes
    ----------
    psd : ndarray
        The two-sided psd that will be used to describe the noise in the signal (in Amps^2/Hz)
    psd0 : float
        The value of the inputted PSD at the zero frequency bin. Used for ofamp_baseline in the
        case that `OptimumFilter` is initialized with "AC" coupling.
    nbins : int
        The length of the trace/psd/template in bins.
    fs : float
        The sample rate of the data being taken (in Hz).
    df : float
        Equivalent to df/nbins, the frequency spacing of the Fourier Tranforms.
    s : ndarray
        The template converted to frequency space, with the normalization specified 
        by the `integralnorm` parameter in the initialization.
    phi : ndarray
        The optimum filter in frequency space.
    norm : float
        The normalization for the optimum filtered signal.
    v : ndarray
        The signal converted to frequency space.
    signalfilt : ndarray
        The optimum filtered signal in frequency space.
    chi0 : float
        The chi^2 value for just the signal part.
    chit_withdelay : ndarray
        The fitting part of the chi^2 for `ofamp_withdelay`.
    amps_withdelay : ndarray
        The possible amplitudes for `ofamp_withdelay`.
    chi_withdelay : ndarray
        The full chi^2 for `ofamp_withdelay`.
    signalfilt_td : ndarray
        The filtered signal converted back to time domain.
    templatefilt_td : ndarray
        The filtered template converted back to time domain.
    times : ndarray
        The possible time shift values.
    freqs : ndarray
        The frequencies matching the Fourier Transform of the data.
    
    """
    
    def __init__(self, signal, template, psd, fs, coupling="AC", integralnorm=False):
        """
        Initialization of the OptimumFilter class.
        
        Parameters
        ----------
        signal : ndarray
            The signal that we want to apply the optimum filter to (units should be Amps).
        template : ndarray
            The pulse template to be used for the optimum filter (should be normalized
            to a max height of 1 beforehand).
        psd : ndarray
            The two-sided psd that will be used to describe the noise in the 
            signal (in Amps^2/Hz)
        fs : ndarray
            The sample rate of the data being taken (in Hz).
        coupling : str, optional
            String that determines if the zero frequency bin of the psd should be 
            ignored (i.e. set to infinity) when calculating the optimum amplitude. 
            If set to 'AC', then ths zero frequency bin is ignored. If set to anything
            else, then the zero frequency bin is kept. Default is 'AC'.
        integralnorm : bool, optional
            If set to True, then `OptimumFilter` will normalize the template to an integral of 1,
            and any optimum filters will instead return the optimum integral in units of Coulombs. 
            If set to False, then the usual optimum filter amplitudes will be returned (in units 
            of Amps).
        
        """
        
        self.psd = np.zeros(len(psd))
        self.psd[:] = psd
        self.psd0 = psd[0]
        
        if coupling=="AC":
            self.psd[0]=np.inf
            
        self.nbins = signal.shape[-1]
        self.fs = fs
        self.df = self.fs/self.nbins
        
        self.s = fft(template)/self.nbins/self.df
        
        if integralnorm:
            self.s/=self.s[0]
        
        self.phi = self.s.conjugate()/self.psd
        self.norm = np.real(np.dot(self.phi, self.s))*self.df

        self.v = fft(signal, axis=-1)/self.nbins/self.df
        self.signalfilt = self.phi * self.v / self.norm
        
        self.chi0 = None
        
        self.chit_withdelay = None
        self.amps_withdelay = None
        self.chi_withdelay = None
        
        self.signalfilt_td = None
        self.templatefilt_td = None
        
        self.times = None
        self.freqs = None
        
    def update_signal(self, signal):
        """
        Method to update `OptimumFilter` with a new signal if the PSD and template
        are to remain the same.
        
        Parameters
        ----------
        signal : ndarray
            The signal that we want to apply the optimum filter to (units should be Amps).
        
        """
        
        self.v = fft(signal, axis=-1)/self.nbins/self.df
        self.signalfilt = self.phi * self.v / self.norm
        
        self.chi0 = None
        self.chit_withdelay = None
        self.signalfilt_td = None
        self.amps_withdelay = None
        self.chi_withdelay = None
        
    def energy_resolution(self):
        """
        Method to return the energy resolution for the optimum filter.
        
        Returns
        -------
        sigma : float
            The energy resolution of the optimum filter.
        
        """
        
        sigma = 1.0/np.sqrt(self.norm)
        
        return sigma
    
    def time_resolution(self, amp):
        """
        Method to return the time resolution for the optimum filter for a specific fit.
        
        Parameters
        ----------
        amp : float
            The OF amplitude of the fit to use in the time resolution calculation.
            
        Returns
        -------
        sigma : float
            The time resolution of the optimum filter.
            
        """
        
        if self.freqs is None:
            self.freqs = fftfreq(self.nbins, d=1.0/self.fs)
        
        sigma = 1.0/np.sqrt(amp**2 * np.sum((2*np.pi*self.freqs)**2 * np.abs(self.s)**2 / self.psd) * self.df)
        
        return sigma
        
        
    def chi2_nopulse(self):
        """
        Method to return the chi^2 for there being no pulse in the signal.
        
        Returns
        -------
        chi0 : float
            The chi^2 value for there being no pulse.
        
        """
        
        # signal part of chi2
        if self.chi0 is None:
            self.chi0 = np.real(np.dot(self.v.conjugate()/self.psd, self.v)*self.df)
            
        return self.chi0
        
    def chi2_lowfreq(self, amp, t0, fcutoff=10000):
        """
        Method for calculating the low frequency chi^2 of the optimum filter, 
        given some cut off frequency.

        Parameters
        ----------
        amp : float
            The optimum amplitude calculated for the trace (in Amps).
        t0 : float
            The time shift calculated for the pulse (in s).
        fcutoff : float, optional
            The frequency (in Hz) that we should cut off the chi^2 when 
            calculating the low frequency chi^2. Default is 10 kHz.

        Returns
        -------
        chi2low : float
            The low frequency chi^2 value (cut off at fcutoff) for the inputted values.

        """
        
        if self.freqs is None:
            self.freqs = fftfreq(self.nbins, d=1.0/self.fs)
        
        chi2tot = self.df*np.abs(self.v-amp*np.exp(-2.0j*np.pi*t0*self.freqs)*self.s)**2/self.psd

        chi2inds = np.abs(self.freqs)<=fcutoff

        chi2low = np.sum(chi2tot[chi2inds])
        
        return chi2low
        
    def ofamp_nodelay(self):
        """
        Function for calculating the optimum amplitude of a pulse in data with no time
        shifting.
        
        Returns
        -------
        amp : float
            The optimum amplitude calculated for the trace (in Amps) with no time shifting
            allowed.
        chi2 : float
            The chi^2 value calculated from the optimum filter with no time shifting.

        """
        # compute OF amplitude no delay
        amp = np.real(np.sum(self.signalfilt, axis=-1))*self.df

        # signal part of chi2
        if self.chi0 is None:
            self.chi0 = np.real(np.dot(self.v.conjugate()/self.psd, self.v)*self.df)
        
        # fitting part of chi2
        chit = (amp**2)*self.norm

        chi2 = self.chi0 - chit
        
        return amp, chi2
    
    def ofamp_withdelay(self, nconstrain=None, lgcoutsidewindow=False, 
                        pulse_direction_constraint=0):
        """
        Function for calculating the optimum amplitude of a pulse in data with time delay.

        Parameters
        ----------
        nconstrain : int, NoneType, optional
            The length of the window (in bins) to constrain the possible t0 values to, 
            centered on the unshifted trigger. If left as None, then t0 is uncontrained. 
            If `nconstrain` is larger than `self.nbins`, then the function sets `nconstrain` 
            to `self.nbins`, as this is the maximum number of values that t0 can vary over.
        lgcoutsidewindow : bool, optional
            Boolean flag that is used to specify whether the Optimum Filter should look inside 
            `nconstrain` or outside it. If False, the filter will minimize the chi^2 in the 
            bins specified by `nconstrain`, which is the default behavior. If True, then it 
            will minimize the chi^2 in the bins that do not contain the constrained window.
        pulse_direction_constraint : int, optional
            Sets a constraint on the direction of the fitted pulse. If 0, then no constraint on the 
            pulse direction is set. If 1, then a positive pulse constraint is set for all fits. 
            If -1, then a negative pulse constraint is set for all fits. If any other value, then
            an ValueError will be raised.
            
        Returns
        -------
        amp : float
            The optimum amplitude calculated for the trace (in Amps).
        t0 : float
            The time shift calculated for the pulse (in s).
        chi2 : float
            The chi^2 value calculated from the optimum filter.

        """
        
        if self.signalfilt_td is None:
            self.signalfilt_td = np.real(ifft(self.signalfilt*self.nbins, axis=-1))*self.df

        # signal part of chi2
        if self.chi0 is None:
            self.chi0 = np.real(np.dot(self.v.conjugate()/self.psd, self.v)*self.df)

        # fitting part of chi2
        if self.chit_withdelay is None:
            self.chit_withdelay = (self.signalfilt_td**2)*self.norm

        # sum parts of chi2
        if self.chi_withdelay is None:
            chi = self.chi0 - self.chit_withdelay
            self.chi_withdelay = np.roll(chi, self.nbins//2, axis=-1)
        
        if self.amps_withdelay is None:
            self.amps_withdelay = np.roll(self.signalfilt_td, self.nbins//2, axis=-1)
        
        # apply pulse direction constraint
        constraint_mask = _get_pulse_direction_constraint_mask(self.amps_withdelay, 
                                                               pulse_direction_constraint=pulse_direction_constraint)
        
        # find time of best fit
        bestind = _argmin_chi2(self.chi_withdelay, nconstrain=nconstrain, 
                               lgcoutsidewindow=lgcoutsidewindow, constraint_mask=constraint_mask)
        
        if np.isnan(bestind):
            amp = 0.0
            t0 = 0.0
            chi2 = self.chi0
        else:
            amp = self.amps_withdelay[bestind]
            t0 = (bestind-self.nbins//2)/self.fs
            chi2 = self.chi_withdelay[bestind]
        
        return amp, t0, chi2
    
    def ofamp_pileup_iterative(self, a1, t1, nconstrain=None, lgcoutsidewindow=True, 
                               pulse_direction_constraint=0):
        """
        Function for calculating the optimum amplitude of a pileup pulse in data given
        the location of the triggered pulse.

        Parameters
        ----------
        a1 : float
            The OF amplitude (in Amps) to use for the "main" pulse, e.g. the triggered pulse.
        t1 : float
            The corresponding time offset (in seconds) to use for the "main" pulse, e.g. the 
            triggered pulse.
        nconstrain : int, NoneType, optional
            This is the length of the window (in bins) out of which to constrain the possible 
            t2 values to for the pileup pulse, centered on the unshifted trigger. If left as 
            None, then t2 is uncontrained. The value of nconstrain2 should be less than nbins.
        lgcoutsidewindow : bool, optional
            Boolean flag that is used to specify whether `OptimumFilter` should look for the 
            pileup pulse inside the bins specified by `nconstrain` or outside them. If True, 
            the filter will minimize the chi^2 in the bins outside the range specified by
            `nconstrain`, which is the default behavior. If False, then it will minimize the 
            chi^2 in the bins inside the constrained window specified by `nconstrain`.
        pulse_direction_constraint : int, optional
            Sets a constraint on the direction of the fitted pulse. If 0, then no constraint on the 
            pulse direction is set. If 1, then a positive pulse constraint is set for all fits. 
            If -1, then a negative pulse constraint is set for all fits. If any other value, then
            an ValueError will be raised.
            
        Returns
        -------
        a2 : float
            The optimum amplitude calculated for the pileup pulse (in Amps).
        t2 : float
            The time shift calculated for the pileup pulse (in s)
        chi2 : float
            The chi^2 value calculated for the pileup optimum filter.

        """
        
        if self.freqs is None:
            self.freqs = fftfreq(self.nbins, d=1.0/self.fs)
        
        if self.signalfilt_td is None:
            self.signalfilt_td = np.real(ifft(self.signalfilt*self.nbins, axis=-1))*self.df
        
        templatefilt_td = np.real(ifft(np.exp(-2.0j*np.pi*self.freqs*t1)*self.phi*self.s*self.nbins))*self.df
        
        if self.times is None:
            self.times = np.arange(-(self.nbins//2), self.nbins//2+self.nbins%2)/self.fs
            
        # signal part of chi^2
        if self.chi0 is None:
            self.chi0 = np.real(np.dot(self.v.conjugate()/self.psd, self.v))*self.df
        
        a2s = self.signalfilt_td - a1*templatefilt_td/self.norm
        
        if t1<0:
            t1ind = int(t1*self.fs+self.nbins)
        else:
            t1ind = int(t1*self.fs)
        
        # do a1 part of chi2
        chit = a1**2*self.norm - 2*a1*self.signalfilt_td[t1ind]*self.norm

        # do a1, a2 combined part of chi2
        chil = a2s**2*self.norm + 2*a1*a2s*templatefilt_td - 2*a2s*self.signalfilt_td*self.norm

        # add all parts of chi2
        chi = self.chi0 + chit + chil

        a2s = np.roll(a2s, self.nbins//2)
        chi = np.roll(chi, self.nbins//2)
        
        # apply pulse direction constraint
        constraint_mask = _get_pulse_direction_constraint_mask(a2s, 
                                                               pulse_direction_constraint=pulse_direction_constraint)
        
        # find time of best fit
        bestind = _argmin_chi2(chi, nconstrain=nconstrain, 
                               lgcoutsidewindow=lgcoutsidewindow, constraint_mask=constraint_mask)
        
        if np.isnan(bestind):
            a2 = 0.0
            t2 = 0.0
            chi2 = self.chi0 + chit
        else:
            a2 = a2s[bestind]
            t2 = self.times[bestind]
            chi2 = chi[bestind]
        
        return a2, t2, chi2
        
    def ofamp_pileup_stationary(self, nconstrain=None, lgcoutsidewindow=True):
        """
        Function for calculating the optimum amplitude of a pileup pulse in data, with the assumption
        that the triggered pulse is centered in the trace.

        Parameters
        ----------
        nconstrain : int, optional
            This is the length of the window (in bins) out of which to constrain the possible 
            t2 values to for the pileup pulse, centered on the unshifted trigger. If left as None, 
            then t2 is uncontrained. The value of nconstrain should be less than nbins.
        lgcoutsidewindow : bool, optional
            Boolean flag that is used to specify whether the filter should look for the pileup 
            pulse inside the bins specified by `nconstrain` or outside them. If True, the filter will 
            minimize the chi^2 in the bins outside the range specified by `nconstrain`, which is the 
            default behavior. If False, then it will minimize the chi^2 in the bins inside the 
            constrained window specified by nconstrain.
            
        Returns
        -------
        a1 : float
            The optimum amplitude (in Amps) calculated for the first pulse that was found, which
            is the triggered pulse.
        a2 : float
            The optimum amplitude calculated for the pileup pulse (in Amps).
        t2 : float
            The time shift calculated for the pileup pulse (in s)
        chi2 : float
            The reduced chi^2 value of the fit.

        """
        
        if self.signalfilt_td is None:
            self.signalfilt_td = np.real(ifft(self.signalfilt*self.nbins))*self.df
            
        templatefilt_td = np.real(ifft(self.phi*self.s*self.nbins))*self.df

        if self.times is None:
            self.times = np.arange(-(self.nbins//2), self.nbins//2+self.nbins%2)/self.fs

        # compute OF with delay
        denom = self.norm**2 - templatefilt_td**2
        
        a1s = np.zeros(self.nbins)
        a2s = np.zeros(self.nbins)
        
        # calculate the non-zero freq bins
        a1s[1:] = (self.signalfilt_td[0]*self.norm**2 - self.signalfilt_td[1:]*self.norm*templatefilt_td[1:])/denom[1:]
        a2s[1:] = (self.signalfilt_td[1:]*self.norm**2 - self.signalfilt_td[0]*self.norm*templatefilt_td[1:])/denom[1:]
        
        # calculate the zero freq bins to avoid divide by zero
        a1s[0] = self.signalfilt_td[0]/(2*self.norm)
        a2s[0] = self.signalfilt_td[0]/(2*self.norm)

        # signal part of chi^2
        if self.chi0 is None:
            self.chi0 = np.real(np.dot(self.v.conjugate()/self.psd, self.v))*self.df

        # first fitting part of chi2
        chit = (a1s**2+a2s**2)*self.norm + 2*a1s*a2s*templatefilt_td

        # last part of chi2
        chil = 2*a1s*self.signalfilt_td[0]*self.norm + 2*a2s*self.signalfilt_td*self.norm

        # add all parts of chi2
        chi = self.chi0 + chit - chil
        
        a1s = np.roll(a1s, self.nbins//2)
        a2s = np.roll(a2s, self.nbins//2)
        chi = np.roll(chi, self.nbins//2)
        
        # find time of best fit
        bestind = _argmin_chi2(chi, nconstrain=nconstrain, lgcoutsidewindow=lgcoutsidewindow)

        # get best fit values
        a1 = a1s[bestind]
        a2 = a2s[bestind]
        chi2 = chi[bestind]
        t2 = self.times[bestind]
        
        return a1, a2, t2, chi2

    def ofamp_baseline(self, nconstrain=None, lgcoutsidewindow=False, 
                       pulse_direction_constraint=0):
        """
        Function for calculating the optimum amplitude of a pulse while taking into account
        the best fit baseline. If the window is constrained, the fit uses the baseline taken 
        from the unconstrained best fit and fixes it when looking elsewhere. This is to 
        reduce the shifting of the best fit amplitudes at times far from the true pulse.

        Parameters
        ----------
        nconstrain : int, NoneType, optional
            The length of the window (in bins) to constrain the possible t0 values to, 
            centered on the unshifted trigger. If left as None, then t0 is uncontrained. 
            If `nconstrain` is larger than `self.nbins`, then the function sets `nconstrain` 
            to `self.nbins`, as this is the maximum number of values that t0 can vary over.
        lgcoutsidewindow : bool, optional
            Boolean flag that is used to specify whether the Optimum Filter should look inside 
            `nconstrain` or outside it. If False, the filter will minimize the chi^2 in the 
            bins specified by `nconstrain`, which is the default behavior. If True, then it 
            will minimize the chi^2 in the bins that do not contain the constrained window.
        pulse_direction_constraint : int, optional
            Sets a constraint on the direction of the fitted pulse. If 0, then no constraint on the 
            pulse direction is set. If 1, then a positive pulse constraint is set for all fits. 
            If -1, then a negative pulse constraint is set for all fits. If any other value, then
            an ValueError will be raised.
            
        Returns
        -------
        amp : float
            The optimum amplitude calculated for the trace (in Amps).
        t0 : float
            The time shift calculated for the pulse (in s).
        chi2 : float
            The chi^2 value calculated from the optimum filter.

        """
    
        d = 1/self.df # fourier tranform of constant

        if np.isinf(self.psd[0]):
            phi = self.phi.copy()
            phi[0] = self.s[0]/self.psd0 # don't need to do s.conjugate() since zero freq is real
            norm = np.real(np.dot(phi, self.s))*self.df
        else:
            phi = self.phi
            norm = self.norm

        b1 = np.real(ifft(phi*self.v))*self.nbins*self.df
        b2 = np.real(phi[0]*d)*self.df

        c1 = np.real(self.v[0]*d/self.psd0)*self.df
        c2 = np.abs(d)**2/self.psd0*self.df

        amps = (b1*c2 - b2*c1)/(norm*c2 - b2**2)

        baselines = (c1 - amps * b2)/c2

        chi2 = np.sum(np.abs(self.v)**2 / self.psd)*self.df
        if np.isinf(self.psd[0]):
            chi2+=np.abs(self.v[0])**2 / self.psd0 * self.df # add back the zero frequency bin
            
        chi2+= -2*(amps*b1 + baselines*c1)
        chi2+= amps**2 * norm
        chi2+= 2*amps*baselines*b2
        chi2+= baselines**2 * c2

        bestind = np.argmin(chi2)

        bs = baselines[bestind]

        amps_out = (b1 - bs * b2)/norm

        # recalculated chi2 with baseline fixed to best fit baseline
        chi0 = np.sum(np.abs(self.v)**2 / self.psd)*self.df
        if np.isinf(self.psd[0]):
            chi0+=np.abs(self.v[0])**2 / self.psd0 * self.df # add back the zero frequency bin
        chi0-= 2*bs*c1
        chi0+= bs**2 * c2
        
        chi2 = chi0 - 2*amps_out*b1
        chi2+= amps_out**2 * norm
        chi2+= 2*amps_out*bs*b2

        amps_out = np.roll(amps_out, self.nbins//2, axis=-1)
        chi2 = np.roll(chi2, self.nbins//2, axis=-1)
        
        # apply pulse direction constraint
        constraint_mask = _get_pulse_direction_constraint_mask(amps_out, 
                                                               pulse_direction_constraint=pulse_direction_constraint)
        
        # find time of best fit
        bestind = _argmin_chi2(chi2, nconstrain=nconstrain, 
                               lgcoutsidewindow=lgcoutsidewindow, constraint_mask=constraint_mask)
        if np.isnan(bestind):
            amp = 0
            t0 = 0
            chi2 = chi0
        else:
            amp = amps_out[bestind]
            t0 = (bestind-self.nbins//2)/self.fs
            chi2 = chi2[bestind]

        return amp, t0, chi2

def ofamp(signal, template, inputpsd, fs, withdelay=True, coupling='AC', lgcsigma = False, 
          nconstrain=None, lgcoutsidewindow=False, integralnorm=False):
    """
    Function for calculating the optimum amplitude of a pulse in data. Supports optimum filtering with
    and without time delay.
    
    Parameters
    ----------
    signal : ndarray
        The signal that we want to apply the optimum filter to (units should be Amps). Can be an array
        of traces.
    template : ndarray
        The pulse template to be used for the optimum filter (should be normalized beforehand).
    inputpsd : ndarray
        The two-sided psd that will be used to describe the noise in the signal (in Amps^2/Hz)
    fs : float
        The sample rate of the data being taken (in Hz).
    withdelay : bool, optional
        Determines whether or not the optimum amplitude should be calculate with (True) or without
        (False) using a time delay. With the time delay, the pulse is assumed to be at any time in the trace.
        Without the time delay, the pulse is assumed to be directly in the middle of the trace. Default
        is True.
    coupling : str, optional
        String that determines if the zero frequency bin of the psd should be ignored (i.e. set to infinity)
        when calculating the optimum amplitude. If set to 'AC', then ths zero frequency bin is ignored. If
        set to anything else, then the zero frequency bin is kept. Default is 'AC'.
    lgcsigma : Boolean, optional
        If True, the estimated optimal filter energy resolution will be calculated and returned.
    nconstrain : int, NoneType, optional
        The length of the window (in bins) to constrain the possible t0 values to, centered on the unshifted 
        trigger. If left as None, then t0 is uncontrained. If nconstrain is larger than nbins, then 
        the function sets nconstrain to nbins, as this is the maximum number of values that t0 can vary
        over.
    lgcoutsidewindow : bool, optional
        Boolean flag that is used to specify whether ofamp should look inside nconstrain or outside it. 
        If False, ofamp will minimize the chi^2 in the bins specified by nconstrain, which is the 
        default behavior. If True, then ofamp will minimize the chi^2 in the bins that do not contain the
        constrained window.
    integralnorm : bool, optional
        If set to True, then ofamp will normalize the template to an integral of 1, and ofamp will instead
        return the optimal integral in units of Coulombs. If lgcsigma is set to True, then it will be 
        returned in units of Coulombs as well. If set to False, then the usual optimal filter amplitude
        will be returned (in units of Amps).

    Returns
    -------
    amp : float
        The optimum amplitude calculated for the trace (in Amps).
    t0 : float
        The time shift calculated for the pulse (in s). Set to zero if withdelay is False.
    chi2 : float
        The chi^2 value calculated from the optimum filter.
    sigma : float, optional
        The optimal filter energy resolution (in Amps)
        
    """
    
    psd = np.zeros(len(inputpsd))
    psd[:] = inputpsd
    
    if len(signal.shape)==1:
        signal = signal[np.newaxis, :]
    
    nbins = signal.shape[-1]
    df = fs/nbins

    # take fft of signal and template, divide by nbins to get correct convention 
    v = fft(signal, axis=-1)/nbins/df
    s = fft(template)/nbins/df

    if integralnorm:
        s/=s[0]

    # check for compatibility between PSD and DFT
    if(len(psd) != v.shape[-1]):
        raise ValueError("PSD length incompatible with signal size")

    # if AC coupled, the 0 component of the PSD is non-sensical
    # if DC coupled, ignoring the DC component will still give the correct amplitude
    if coupling == 'AC':
        psd[0]=np.inf

    # find optimum filter and norm
    phi = s.conjugate()/psd
    norm = np.real(np.dot(phi, s))*df
    signalfilt = phi*v/norm

    # calculate the expected energy resolution
    if lgcsigma:
        sigma = 1/(np.dot(phi, s).real*df)**0.5

    if withdelay:
        # compute OF with delay
        # correct for fft convention by multiplying by nbins
        amps = np.real(ifft(signalfilt*nbins, axis=-1))*df

        # signal part of chi2
        chi0 = np.real(np.einsum('ij,ij->i', v.conjugate()/psd, v)*df)

        # fitting part of chi2
        chit = (amps**2)*norm

        # sum parts of chi2
        chi = chi0[:, np.newaxis] - chit

        amps = np.roll(amps, nbins//2, axis=-1)
        chi = np.roll(chi, nbins//2, axis=-1)

        # find time of best fit
        bestind = _argmin_chi2(chi, nconstrain=nconstrain, lgcoutsidewindow=lgcoutsidewindow)

        amp = np.diag(amps[:, bestind])
        chi2 = np.diag(chi[:, bestind])
        t0 = (bestind-nbins//2)/fs

    else:
        # compute OF amplitude no delay
        amp = np.real(np.sum(signalfilt, axis=-1))*df
        t0 = np.zeros(len(amp))

        # signal part of chi2
        chi0 = np.real(np.einsum('ij,ij->i', v.conjugate()/psd, v)*df)

        # fitting part of chi2
        chit = (amp**2)*norm

        chi2 = chi0 - chit
    
    if len(amp)==1:
        amp=amp[0]
        t0=t0[0]
        chi2=chi2[0]
        
    if lgcsigma:
        return amp, t0, chi2, sigma
    else:
        return amp, t0, chi2
    
def ofamp_pileup(signal, template, inputpsd, fs, a1=None, t1=None, coupling='AC',
                 nconstrain1=None, nconstrain2=None, lgcoutsidewindow=True):
    """
    Function for calculating the optimum amplitude of a pileup pulse in data. Supports inputted the
    values of a previously known pulse for increased computational speed, but can be used on its
    own.
    
    Parameters
    ----------
    signal : ndarray
        The signal that we want to apply the optimum filter to (units should be Amps).
    template : ndarray
        The pulse template to be used for the optimum filter (should be normalized beforehand).
    inputpsd : ndarray
        The two-sided psd that will be used to describe the noise in the signal (in Amps^2/Hz)
    fs : float
        The sample rate of the data being taken (in Hz).
    a1 : float, optional
        The OF amplitude (in Amps) to use for the "main" pulse, e.g. the triggered pulse. This 
        should be calculated beforehand using ofamp. This is only used if t1 is also inputted.
    t1 : float, optional
        The corresponding time offset (in seconds) to use for the "main" pulse, e.g. the triggered
        pulse. As with a1, this should be calculated beforehand using ofamp. This is only used if a1
        is also inputted.
    coupling : str, optional
        String that determines if the zero frequency bin of the psd should be ignored (i.e. set to infinity)
        when calculating the optimum amplitude. If set to 'AC', then ths zero frequency bin is ignored. If
        set to anything else, then the zero frequency bin is kept. Default is 'AC'.
    nconstrain1 : int, NoneType, optional
        If t1 is left as None, this is the length of the window (in bins) to constrain the possible 
        t1 values to for the first pulse, centered on the unshifted trigger. If left as None, then 
        t1 is uncontrained. If nconstrain1 is larger than nbins, then the function sets nconstrain1 to 
        nbins, as this is the maximum number of values that t1 can vary over. This is only used if
        a1 or t1 is not given.
    nconstrain2 : int, NoneType, optional
        This is the length of the window (in bins) out of which to constrain the possible 
        t2 values to for the pileup pulse, centered on the unshifted trigger. If left as None, then 
        t2 is uncontrained. The value of nconstrain2 should be less than nbins.
    lgcoutsidewindow : bool, optional
        Boolean flag that is used to specify whether ofamp_pileup should look for the pileup pulse inside the
        bins specified by  nconstrain2 or outside them. If True, ofamp will minimize the chi^2 in the bins outside
        the range specified by nconstrain2, which is the default behavior. If False, then ofamp will minimize the
        chi^2 in the bins inside the constrained window specified by nconstrain2.

    Returns
    -------
    a1 : float
        The optimum amplitude (in Amps) calculated for the first pulse that was found, which
        is generally the triggered pulse.
    t1 : float
        The time shift calculated for the first pulse that was found (in s)
    a2 : float
        The optimum amplitude calculated for the pileup pulse (in Amps).
    t2 : float
        The time shift calculated for the pileup pulse (in s)
    chi2 : float
        The chi^2 value calculated for the pileup optimum filter.
        
    """

    psd = np.zeros(len(inputpsd))
    psd[:] = inputpsd
    
    nbins = len(signal)
    df = fs/nbins
    freqs = fftfreq(nbins, d=1.0/fs)
    omega = 2.0*np.pi*freqs
    
    if a1 is None or t1 is None:
        a1, t1, _ = ofamp(signal, template, psd, fs, withdelay=True, 
                          coupling=coupling, nconstrain=nconstrain1)
    
    # take fft of signal and template, divide by nbins to get correct convention 
    v = fft(signal)/nbins/df
    s = fft(template)/nbins/df

    # check for compatibility between PSD and DFT
    if(len(psd) != len(v)):
        raise ValueError("PSD length incompatible with signal size")
    
    # if AC coupled, the 0 component of the PSD is non-sensical
    # if DC coupled, ignoring the DC component will still give the correct amplitude
    if coupling == 'AC':
        psd[0]=np.inf

    # find optimum filter and norm
    phi = s.conjugate()/psd
    norm = np.real(np.dot(phi, s))*df
    signalfilt = phi*v/norm
    
    signalfilt_td = np.real(ifft(signalfilt*nbins))*df
    templatefilt_td = np.real(ifft(np.exp(-1.0j*omega*t1)*phi*s*nbins))*df
    
    times = np.arange(-(nbins//2), nbins//2+nbins%2)/fs
    
    # compute OF with delay
    # correct for fft convention by multiplying by nbins
    a2s = signalfilt_td - a1*templatefilt_td/norm
    
    # signal part of chi^2
    chi0 = np.real(np.dot(v.conjugate()/psd, v))*df
    
    # first fitting part of chi2
    chit = (a1**2+a2s**2)*norm + 2*a1*a2s*templatefilt_td
    
    if t1<0:
        t1ind = int(t1*fs+nbins)
    else:
        t1ind = int(t1*fs)
        
    # last part of chi2
    chil = 2*a1*signalfilt_td[t1ind]*norm + 2*a2s*signalfilt_td*norm
    
    # add all parts of chi2
    chi = chi0 + chit - chil

    a2s = np.roll(a2s, nbins//2)
    chi = np.roll(chi, nbins//2)
    
    # find time of best fit
    bestind = _argmin_chi2(chi, nconstrain=nconstrain2, lgcoutsidewindow=lgcoutsidewindow)

    # get best fit values
    a2 = a2s[bestind]
    chi2 = chi[bestind]
    t2 = times[bestind]
    
    return a1, t1, a2, t2, chi2

def ofamp_pileup_stationary(signal, template, inputpsd, fs, coupling='AC', nconstrain=None, lgcoutsidewindow=False):
    """
    Function for calculating the optimum amplitude of a pileup pulse in data, with the assumption
    that the triggered pulse is centered in the trace.
    
    Parameters
    ----------
    signal : ndarray
        The signal that we want to apply the optimum filter to (units should be Amps).
    template : ndarray
        The pulse template to be used for the optimum filter (should be normalized beforehand).
    inputpsd : ndarray
        The two-sided psd that will be used to describe the noise in the signal (in Amps^2/Hz)
    fs : float
        The sample rate of the data being taken (in Hz).
    coupling : str, optional
        String that determines if the zero frequency bin of the psd should be ignored (i.e. set to infinity)
        when calculating the optimum amplitude. If set to 'AC', then ths zero frequency bin is ignored. If
        set to anything else, then the zero frequency bin is kept. Default is 'AC'.
    nconstrain : int, NoneType, optional
        This is the length of the window (in bins) out of which to constrain the possible 
        t2 values to for the pileup pulse, centered on the unshifted trigger. If left as None, then 
        t2 is uncontrained. The value of nconstrain should be less than nbins.
    lgcoutsidewindow : bool, optional
        Boolean flag that is used to specify whether the function should look for the pileup pulse inside the
        bins specified by  nconstrain or outside them. If True, ofamp will minimize the chi^2 in the bins outside
        the range specified by nconstrain, which is the default behavior. If False, then ofamp will minimize the
        chi^2 in the bins inside the constrained window specified by nconstrain.

    Returns
    -------
    a1 : float
        The optimum amplitude (in Amps) calculated for the first pulse that was found, which
        is the triggered pulse.
    a2 : float
        The optimum amplitude calculated for the pileup pulse (in Amps).
    t2 : float
        The time shift calculated for the pileup pulse (in s)
    chi2 : float
        The reduced chi^2 value of the fit.
        
    """
    
    psd = np.zeros(len(inputpsd))
    psd[:] = inputpsd
    
    nbins = len(signal)
    df = fs/nbins
    
    # take fft of signal and template, divide by nbins to get correct convention 
    v = fft(signal)/nbins/df
    s = fft(template)/nbins/df

    # check for compatibility between PSD and DFT
    if(len(psd) != len(v)):
        raise ValueError("PSD length incompatible with signal size")
    
    # if AC coupled, the 0 component of the PSD is non-sensical
    # if DC coupled, ignoring the DC component will still give the correct amplitude
    if coupling == 'AC':
        psd[0]=np.inf

    # find optimum filter and norm
    phi = s.conjugate()/psd
    norm = np.real(np.dot(phi, s))*df
    signalfilt = phi*v/norm
    
    signalfilt_td = np.real(ifft(signalfilt*nbins))*df * norm
    templatefilt_td = np.real(ifft(phi*s*nbins))*df
    
    times = np.arange(-(nbins//2), nbins//2+nbins%2)/fs
    
    # compute OF with delay
    denom = norm**2 - templatefilt_td**2
    
    a1s = np.zeros(nbins)
    a2s = np.zeros(nbins)
    
    # calculate the non-zero freq bins
    a1s[1:] = (signalfilt_td[0]*norm - signalfilt_td[1:]*templatefilt_td[1:])/denom[1:]
    a2s[1:] = (signalfilt_td[1:]*norm - signalfilt_td[0]*templatefilt_td[1:])/denom[1:]
    
    # calculate the zero freq bins to avoid divide by zero
    a1s[0] = signalfilt_td[0]/(2*norm**2)
    a2s[0] = signalfilt_td[0]/(2*norm**2)
    
    # signal part of chi^2
    chi0 = np.real(np.dot(v.conjugate()/psd, v))*df
    
    # first fitting part of chi2
    chit = (a1s**2+a2s**2)*norm + 2*a1s*a2s*templatefilt_td
        
    # last part of chi2
    chil = 2*a1s*signalfilt_td[0] + 2*a2s*signalfilt_td
    
    # add all parts of chi2
    chi = chi0 + chit - chil
    
    a1s = np.roll(a1s, nbins//2)
    a2s = np.roll(a2s, nbins//2)
    chi = np.roll(chi, nbins//2)
    
    # find time of best fit
    bestind = _argmin_chi2(chi, nconstrain=nconstrain, lgcoutsidewindow=lgcoutsidewindow)

    # get best fit values
    a1 = a1s[bestind]
    a2 = a2s[bestind]
    chi2 = chi[bestind]
    t2 = times[bestind]
    
    return a1, a2, t2, chi2
    

def of_nSmB_fftTemplate(sTemplatet,bTemplatet):
    """
    Parameters
    ----------
    sTemplatet : ndarray
        The n templates for the signal (should be normalized to max(temp)=1)
        Dimensions: (time bins) X ()
    bTemplatet : ndarray
        The m templates for the background (should be normalized to max(temp)=1)
        Dimensions: (time bins) X (m)
        
    Returns
    -------
    sbTemplatef : ndarray
        Frequency domain templates for the signal and background (should be normalized to max(temp)=1)
        Dimensions: (time bins) X (n + m)
    sbTemplatet : ndarray
        Time domain templates for the signal and background (should be normalized to max(temp)=1)
        Dimensions: (time bins) X (n + m)
    
    """
    
    sTemplateShape = sTemplatet.shape
    nt = sTemplateShape[0]
    
    #=== Concatenate signal and background template matrices ====
    catAxis = 1
    sbTemplatet = np.concatenate((sTemplatet, bTemplatet), axis=catAxis)

    #=== FFT of Template ==================================================
    sbTemplatef = np.fft.fft(sbTemplatet,axis=0)/nt
    
    return sbTemplatef, sbTemplatet

def of_nSmB_getWf(sbTemplatef,psddnu,nS,nB,nt):
    
    #=== Creation of Filter and Weighting Matrices ========================
    # initialize:
    #   1) filter matrix in fourier domain
    #   2) weighting matrix in fourier domain
    
    nSB=nS+nB

    # rotate indices for easier matrix multiplication
    OFfiltf_l = np.zeros((nSB,nt),dtype=complex)
    Wf_l = np.zeros((nSB,nSB,nt), dtype=complex)
            
    for jr in range(nSB):
        conjTemp = np.conj(sbTemplatef[:,jr]);
        conjTemp2= np.expand_dims(conjTemp,axis=1)
        OFfiltf_l[jr,:] = np.squeeze(conjTemp2/psddnu);
        for jc in range(nSB):
            conjTemp3 = sbTemplatef[:,jc]
            conjTemp4 = np.expand_dims(conjTemp3,axis=1)
            Wf_l[jr,jc,:]= np.squeeze(conjTemp2/psddnu*conjTemp4);

    # sum Wf along frequency dimension
    # for later calculations
    Wf_l_summed = np.real(np.sum(Wf_l,axis=2))
            
    return Wf_l, Wf_l_summed, OFfiltf_l

def of_nSmB_getWt(Wf_l, Wf_l_summed, Wt_l,nt,combInd=2^20, bindelay=0, bitComb=None,bitMask=None):
    
    
    # check if bitMask has been supplied
    # and if not create it from combInd and bitComb
    if bitMask is None:
        bitMask = np.asarray(bitComb[combInd])
    
    #print('bitMask=', bitMask)
    #print('shape(bitMask)=', np.shape(bitMask))

    # check if the signal is forced to zero
    if (bitMask[0]==1):
        nS = 1
    else:
        nS = 0
    
    # number of signal and backgrounds remaining in fit
    nSBMask = np.sum(bitMask)
    # get indices of the nonzero elements in the mask
    indexBitMask = np.squeeze(np.nonzero(bitMask)) 
    #print('shape(indexBitMask)=',np.shape(indexBitMask))
    #print('indexBitMask=',indexBitMask)
    
    #start = time()
    #WfMask = Wf_l[np.ix_(indexBitMask,indexBitMask)]
    #WfMask = Wf_l[indexBitMask][:,indexBitMask]
    #print(f"time of slice of_nSmB_getWt = ",time()-start)
    
    #print('shape(Wf)=',np.shape(Wf_l))
    #print('shape(WfMask)=',np.shape(WfMask))
    
    # make Wt directly
    #start = time()

    Wt_l2 = np.zeros((nSBMask,nSBMask))

    
    # make the signal-background horizontal piece (row 0 to nS)
        
    try:
        tempIFFT = Wt_l[np.ix_(indexBitMask[0:nS], indexBitMask[0:nSBMask])]
    except IndexError as error:
        print('shape(Wf)=',np.shape(Wf_l))
        print('indexBitMask[0:nS]=',indexBitMask[0:nS])
        print('indexBitMask[0:nSBMask]=',indexBitMask[0:nSBMask])
        
    #print(f"time of ifft of_nSmB_getWt = ",time()-start)

    #start = time()

    #print('shape(tempIFFT) = ', np.shape(tempIFFT))
    #print('shape(transpose(tempIFFT) = ', np.shape(np.transpose(tempIFFT)))
    #print('shape(transpose(tempIFFT[:,:,bindelay]) = ', np.shape(np.transpose(tempIFFT[:,:,bindelay])))
    #print('shape(Wt_l2[0:nSB,0:nS]) = ', np.shape(Wt_l2[0:nSB,0:nS]))
    
    Wt_l2[0:nS,0:nSBMask]=np.real(tempIFFT[:,:,bindelay])
    
    #print(f"time of real Wt_l2 of_nSmB_getWt = ",time()-start)

    #start = time()
    # make the signal-background vertical piece (column 0 to nS)
    Wt_l2[0:nSBMask,0:nS]=np.real(np.transpose(tempIFFT[:,:,bindelay]))
    
    #print(f"time of transpose Wt_l2 of_nSmB_getWt = ",time()-start)

    #start = time()
    # make the top (signal-signal) corner (partial overwriting)
    Wt_l2[0:nS,0:nS] = Wf_l_summed[np.ix_(indexBitMask[0:nS], indexBitMask[0:nS])]
    
    #print(f"time of sum Wt_l2 of_nSmB_getWt = ",time()-start)

    #start = time()

    #make the bottom (background-background) corner
    Wt_l2[nS:nSBMask,nS:nSBMask] = Wf_l_summed[np.ix_(indexBitMask[nS:nSBMask], indexBitMask[nS:nSBMask])];
    
    # make the signal-background piece
    
    #print(f"time of Wt_l2 of_nSmB_getWt = ",time()-start)

    iWt_l2 =np.linalg.pinv(Wt_l2)
    
    return Wt_l2, iWt_l2
    

    
def of_nSmB_setup(sTemplatet,bTemplatet,psd,fs,lgcforcepolarity=False, polarity = 'positive'):
    """
    The setup function for OF_nSmB_inside
        
    Parameters
    ----------
    sTemplatet : ndarray
        The n templates for the signal (should be normalized to max(temp)=1)
        Dimensions: (time bins) X ()
    bTemplatet : ndarray
        The m templates for the background (should be normalized to max(temp)=1)
        Dimensions: (time bins) X (m)
    psd : ndarray
        Two-sided psd that will be used to describe the noise on the pulse (in Amps^2/Hz)
        Converted to one sided immediately below for the matrix generation
        Dimensions: (freq bins = time bins) X ()
    fs : 
        Sample rate in Hz
    lgcforcepolarity: bool, optional
        true if the OF forces all background amplitudes to be of a certain polarity (default=True)
    polarity: string, optional
        string (either 'positive' or 'negative') to indicate the direction of pulses (default='positive')
        
        
        
    Returns
    -------
    psddnu : ndarray
        Two-sided psd multiplied by dnu (in Amps^2)
        Dimensions: (time bins) X (1)
    OFfiltf : ndarray
        The n templates for the signal (should be normalized to max(temp)=1)
        Dimensions: (n + m) X (time bins)
    sbTemplatef : ndarray
        Frequency domain templates for the signal and background (should be normalized to max(temp)=1)
        Dimensions: (time bins) X (n + m)
    sbTemplatet : ndarray
        Time domain templates for the signal and background (should be normalized to max(temp)=1)
        Dimensions: (time bins) X (n + m)
    iWt : ndarray
        Inverse time-domain weighting matrix
        Dimensions: (n + m) X (n + m) X (time bins)
    iBB : ndarray
        Inverse time-domain weighting matrix for background only fit
        Dimensions: (m) X (m) X (time bins)
    nS : int
        Number of signal templates
    nB : int
        Number of background templates
    
    
    
    """""

    lgc_verbose = False
    
    sTemplatet = np.expand_dims(sTemplatet,1)
    sTemplateShape = sTemplatet.shape
    nt = sTemplateShape[0]
    
    nS = int(sTemplateShape[1])
    bTemplateShape = bTemplatet.shape
    nB = int(bTemplateShape[1])
    
    nSB=nS+nB
    
    #=== DAQ Setup ===
    dt = float(1)/fs
    dnu = float(1)/(nt*dt)
   
    
    # convert psd to units of A^2 instead of A^2/hz
    # and renormalize to single sided normalization
    #psddnu=np.expand_dims(psd,1)*dnu*2;
    psddnu=np.expand_dims(psd,1)*dnu;
    
    # make the 0 bin of the PSD equivalent to the first bin
    psddnu[0] = psddnu[1]
    
    #=== Concatenate signal and background template matrices and take FFT====
    
    sbTemplatef, sbTemplatet = of_nSmB_fftTemplate(sTemplatet, bTemplatet)
    
    #print('shape of sbTemplatef',np.shape(sbTemplatef))
    #print('shape of psddnu',np.shape(psddnu))
    
    Wf, Wfsummed, OFfiltf = of_nSmB_getWf(sbTemplatef,psddnu,nS,nB,nt)
    
    # === Switch Weighting Matrix to time domain ===
    Wt_l=np.fft.ifft(Wf,axis=2)*nt
    
    # make a copy to manipulate
    Wt = Wt_l.copy()
    Wt = np.real(Wt)
    # the elements which share a time domain delay contain only the
    # ifft value corresponding to t0=0, i.e. the zero element.
    # these elements are the signalXsignal and the backgroundXbackground

    # signal-signal piece
    # the piece being repeated has dimensions of nSxnS
    # this makes it nSxnSxnt
    noTimeShiftSSMat = Wt[0:nS,0:nS,0,None]
    Wt[0:nS,0:nS,:] = noTimeShiftSSMat@np.ones((1,nt))

    #background-background piece
    noTimeShiftBBMat = Wt[nS:nSB,nS:nSB,0,None]
    Wt[nS:nSB,nS:nSB,:] = noTimeShiftBBMat@np.ones((1,nt))
    
    # signal-background piece is the ifft
    # no need for added manipulation

    # background-signal piece needs to be flipped in time
    # since Wt is symmetric, build it from the top
    # section
    for jr in range(nSB):
        for jc in range(nSB):
            if jr>jc:
                Wt[jr,jc,:] = Wt[jc,jr,:]

    # === Invert Weighting Matrix ===
    # create the inverted weighting matrices
    # as a function of the time offset
    iWt= np.zeros((nSB,nSB,nt))
    
    for jt in range(nt):
        # the regular inv function had a problematic level of numerical jitter
        # e.g. the chi2(t0) could be negative for some t0
        # so use pseudo inverse which has not exhibited
        # any numerical jitter
        iWt[:,:,jt]=np.linalg.pinv(Wt[:,:,jt]);

    
    #start = time()
    # test the new function
    #Wt2, iWt2 = of_nSmB_getP(sbTemplatef,psddnu,nS,nB,nt, combInd=0, bindelay=0)
    #print('time of of_nSmB_getP = ',time()-start)
        
    # check if matrices are the same within tolerance
    #lgcClose = np.allclose(iWt2, iWt[:,:,0],rtol=0, atol=1e-20)    

    # if lgcforcepolarity, compute the other 2^nB
    # matrices analogous to iWt
    
    bitComb = [list(i) for i in itertools.product([0,1],repeat=nSB)]
    if lgcforcepolarity:
    # create list of all binary combinations for number of bits of nB
        numComb = len(bitComb)
        # temporarily set this to a small number to head off memory problems
        #numComb = 5
        for iComb in range(numComb):
            print('test')
        
        
    # === Invert the background-background matrix ===
    BB = np.squeeze(noTimeShiftBBMat)
    iBB = np.linalg.pinv(np.squeeze(noTimeShiftBBMat))
    
    
    return psddnu, OFfiltf, Wf, Wfsummed, Wt_l, sbTemplatef, sbTemplatet, iWt, iBB, BB, nS, nB, bitComb

def of_nSmB_inside(pulset,OFfiltf, Wf_l, Wf_l_summed, Wt_l, sbTemplatef,sbTemplate,iWt,iBB,BB,psddnu,fs,ind_window,nS,nB, bitComb,
                   background_templates_shifts=None,lgc_interp=False,lgcplot=False, lgcsaveplots=False):
    
    """
    Performs all the calculations for an individual pulse
    
    Parameters
    ----------
    pulset : ndarray
        The signal that we want to apply the optimum filter to (units should be Amps).
        Dimensions: (time bins) X ()
    OFfiltf : ndarray
        The n templates for the signal (should be normalized to max(temp)=1)
        Dimensions: (n + m) X (time bins)
    sbTemplatef : ndarray
        The n templates for the signal (should be normalized to max(temp)=1)
        Dimensions: (n + m) X (freq bins = time bins)
    sbTemplate : ndarray
        The m templates for the background (should be normalized to max(temp)=1)
        Dimensions: (time bins) X (n + m)
    iWt : ndarray
        Inverse time-domain weighting matrix
        Dimensions: (n + m) X (n + m) X (time bins)
    iBB : ndarray
        Inverse time-domain weighting matrix for background only fit
        Dimensions: (m) X (m) X (time bins)
    psddnu : ndarray
        Two-sided psd multiplied by dnu (in Amps^2)
        Dimensions: (time bins) X 1
    fs : float
        Sample rate in Hz
        Dimensions: 1
    indwindow: ndarray
        Indices over which one should search for the minimum chi2 of the signal template 
        Dimensions: 1 X (time bins)
    nS : int
        Number of signal templates
        Dimensions: 1
    nB : int
        Number of background templates
        Dimensions: 1
        

    Returns
    -------
    aminsqueeze : ndarray
        Best fit amplitude for n signals and m backgrounds
        Dimensions: (n+m) X 0
    tdelmin : ndarray
        The best fit time delay of the n signals
        Dimensions:  1 X 0
    chi2min: tuple
        The chi^2 of the fit
        Dimensions: 1
    Pulset_BF: ndarray
        The time domain pulse with the best fit m backgrounds subtracted off
        Dimensions: (time bins) X (1)
    a0: tuple
        The best fit amplitudes with the signal template constrained to t0=1 
        Dimensions: 1
    chi20: tuple
        The chi^2 with the signal template constrained to t0=1
        Dimensions: 1
        
    History
    -------
    2012 - M Pyle, B Serfass - initial commit to matlab
        http://titus.stanford.edu:8080/git/blob/?f=utilities/fitting/OF_nSmB.m&r=Analysis/MatCAP.git&h=master
    2018/12/5 - B Page  - ported to python
                        - no chi2 interpolation implemented
                        - added pulse polarity constraint
    
    """

    lfIndex = 500

    
    lgcplotcheck=False
    
    
    
    # === Input Dimensions ===
    pulset = np.expand_dims(pulset,1)
    pulset = pulset.T

    pulsetShape = pulset.shape
    nt = pulsetShape[1]
    OFfiltfShape = OFfiltf.shape
    nSB = OFfiltfShape[0]
    
    # === DAQ Setup ===
    dt = float(1)/fs
    dnu = float(1)/(nt*dt)

    nu = np.arange(0.,float(nt))*dnu
    lgc= nu> nt*dnu/2
    nu[lgc]= nu[lgc]-nt*dnu
    
    omega= (2*np.pi)*nu
    
    # === FFT Pulses ===
    pulsef = np.fft.fft(pulset,axis=1)/nt

    # === Apply the Optimum Filter ===
    Pfiltf = np.zeros((nSB,nt),dtype=complex)
    
    for jT in range(nSB):
        Pfiltf[jT] = OFfiltf[None,jT,:]*pulsef
    
    #=== invert FFT ===
    # Here we want to be careful ... only the signal templates are shifted
    # in time. The background traces are fixed ... thus the only term that
    # matters is the sum (no ifft)
    
    Pfiltt = np.zeros((nSB,nt))
    Pfiltt[0:nS] = np.real(np.fft.ifft(Pfiltf[0:nS],axis=1))*nt
    
    # create sum for background
    backgroundSum=np.real(np.sum(Pfiltf[nS:nSB], axis=1, keepdims=True))
    
    # change backgroundSum (nBx1) into new Mat (nBxnt) where column is just repeated
    backgroundSumExpand = backgroundSum@np.ones((1,nt))
    
    Pfiltt[nS:nSB] = backgroundSumExpand
    Pfiltt2 = np.expand_dims(Pfiltt,axis=2)
    
    # === Fit with only the background templates ===
    # analytical best fit background amplitudes
    bOnlyA = iBB@backgroundSum
    
    # make just the background template in time and frequency
    bTemplate = sbTemplate[:,nS:nSB]
    bTemplatef = sbTemplatef[nS:nSB,:]
    
    # calc chi2 of background only fit
    bestFitBOnly = bTemplate@bOnlyA
    # make residual (pulse minus background best fit)
    residBOnly= pulset.T - bestFitBOnly
    # take FFT of residual
    residBOnlyf = np.fft.fft(residBOnly,axis=0)/nt
    # calc chi2
    chi2BOnly = np.real(np.sum(np.conj(residBOnlyf)/psddnu.T*residBOnlyf,0))    
    
    
    
    ###### ==== start background only constraint   ====== ######    
        
    # find which amplitudes were positive in the original background fit
    
    bitCombFitBackground = np.squeeze(np.asarray(bOnlyA < 0,dtype=int))
    
    # allow the DC template (the last background fit) and the 
    # slope (the second to last background) be any polarity
    bitCombFitBackground[-1] = 1
    bitCombFitBackground[-2] = 1
    
    # the following line is from OLD CODE, where the constraint for the background
    # was based off the signal fit. Now were are basing this constraint off the original
    # background fit
    # get the bitComb for background only
    # bitCombFitBackground = bitCombFitFinal[nS:]
    
    
    #print('bitCombFitFinal=',bitCombFitFinal)
    #print('shape bitCombFitBackground', np.shape(bitCombFitBackground))

    numBCon = np.sum(bitCombFitBackground)
    indexBitMaskBackground = np.squeeze(np.nonzero(bitCombFitBackground))
    
    tempBackgroundSum  = backgroundSum[np.ix_(indexBitMaskBackground)]
    
    BB_Mask = BB[np.ix_(indexBitMaskBackground, indexBitMaskBackground)]
    iBB_Mask = np.linalg.pinv(BB_Mask)
    
    
    #tempiBB = iBB[np.ix_(indexBitMaskBackground, indexBitMaskBackground)]
    #print('np.shape(tempiBB)=',np.shape(tempiBB)) 

    # === Fit with only the background templates with only masked backgrounds active===
    # analytical masked best fit background amplitudes
    
    
    bOnlyAConMask = iBB_Mask@backgroundSum[np.ix_(indexBitMaskBackground)]
    
    #print('bOnlyAConMask=', bOnlyAConMask)

    bOnlyACon = np.zeros((nSB-nS,1))
    bOnlyACon[np.ix_(indexBitMaskBackground)] = bOnlyAConMask
    
    #print('bOnlyA=', bOnlyA)
    #print('bOnlyACon=', bOnlyACon)
    
    # calc chi2 of constrained background only fit
    bestFitBOnlyCon = bTemplate@bOnlyACon
    # make residual (pulse minus background best fit)
    residBOnlyCon= pulset.T - bestFitBOnlyCon
    # take FFT of residual
    residBOnlyfCon = np.fft.fft(residBOnlyCon,axis=0)/nt
    # calc chi2
    chi2BOnlyCon = np.real(np.sum(np.conj(residBOnlyfCon)/psddnu.T*residBOnlyfCon,0))
    chi2BOnlyCon_LF = np.real(np.sum(np.conj(residBOnlyfCon[0:lfIndex])/psddnu.T[0:lfIndex]*residBOnlyfCon[0:lfIndex],0))
    
    bminsqueezeNew = np.squeeze(bOnlyACon)
    

    
    ###### ==== end background only constraint   ====== ######
    
    
    # === Multiply by weighting matrix to get amplitudes at all times ===
    
    # a faster approach
    # change iWt to (jtXnSBXnSB)
    iWtN = np.moveaxis(iWt,2,0)
    
    # change Pfiltt2 to (jtX2X1)
    Pfiltt2N = np.moveaxis(Pfiltt2,1,0)
    a_tN = np.matmul(iWtN,Pfiltt2N)
    a_tN = np.squeeze(a_tN)
    a_t = a_tN.T
    #print('shape(a_t)=',np.shape(a_t))
    #print('shape(Pfiltt)=', np.shape(Pfiltt))
    #=== calculate chi^2 for all time using trick ===
    # first calculate the chi2 component which is independent of the time delay
    chi2base = np.real(np.sum(np.conj(pulsef)/psddnu*pulsef,1))

    chi2base_LF = np.real(np.sum(np.conj(pulsef[:,0:lfIndex])/psddnu[:,0:lfIndex]*pulsef[:,0:lfIndex],1))


    
    #calculate the component which is a function of t0
    chi2t0 = np.sum(a_t*Pfiltt,0)

    chi2_t=chi2base-chi2t0
    #print('shape(chi2_t)=',np.shape(chi2_t))

    #=== Save and Output 0 delay values ===     
    a0 = a_t[:,0]
    chi20=chi2_t[0]
    
    # === Constrain best guess based on ind_window ===
    # sometimes the window given wraps around to negative values
    # convert to all positive values
    lgcneg = ind_window<0
    ind_window[lgcneg] = nt+ind_window[lgcneg]

    #finally ensure that nothing is larger than nt
    lgc_toobig= ind_window >= nt;
    ind_window[lgc_toobig]=np.mod(ind_window[lgc_toobig],nt)    
    
    # find the chi2 minimum
    chi2min= np.amin(chi2_t[ind_window])
    ind_tdel_sm = np.argmin(chi2_t[ind_window])
    #print('chi2min=', chi2min)
    ind_tdel=ind_window[:,ind_tdel_sm]
        
    # plot the chi2_t to check for absolute minima without numerical jitter
    if lgcplotcheck:
        plt.figure(figsize=(12, 7))
        plt.plot(np.arange(nt), chi2_t, '.b');
        plt.xlabel('bin offset')
        plt.ylabel('$\chi^2$')
        plt.grid(which='both')
        if background_templates_shifts is not None:
            for ii in range(nB):
                plt.axvline(x=background_templates_shifts[ii])
                
        plt.figure(figsize=(6,3))
        plt.plot(np.arange(nt), chi2_t,'.');
        plt.xlabel('bin offset')
        plt.ylabel('$\chi^2$')
        plt.grid(which='minor', linestyle='--')
        if background_templates_shifts is not None:
            for ii in range(nB):
                plt.axvline(x=background_templates_shifts[ii])
        plt.xlim([ind_tdel - 10, ind_tdel + 10])
            
        
    #output the lowest chi2 value on the digitized grid 
    amin = a_t[:,ind_tdel]
    tdelmin = ((ind_tdel) - nt*(ind_tdel>nt/2 ))*dt
    
    ###### ==== test positive only constraint ====== ######
    chi2New = np.zeros((nt,1))
    chi2New_LF = np.zeros((nt,1))

    a_tsetNew = np.zeros((nSB,nt))
    bitCombFitVec = np.zeros((nSB,nt),dtype=int)
    
    for ii in range(nt):
        
        #ii= 5230
        # first do the fit with all backgrounds floating
        
        # combInd (2**nSB -1)  for all positive
        # (i.e. bitComb[combInd] will
        # have 1s for the backgrounds
        # we want to remove from fit)
        Wt_tset, iWt_tset = of_nSmB_getWt(Wf_l, Wf_l_summed, Wt_l,nt,combInd=((2**nSB)-1),
                                    bindelay=ii,bitComb=bitComb,bitMask=None)
               
        
        # change iWt to (jtXnSBXnSB)
        #iWtN = np.moveaxis(iWt_tset,2,0)
        # change Pfiltt2 to (jtX2X1)
        Pfiltt2_tset = Pfiltt2N[ii]
        #print('shape(Pfiltt2_tset)=',np.shape(Pfiltt2_tset))
        a_tset = np.matmul(iWt_tset,Pfiltt2_tset)
        
        #record if the amplitudes are positive
        #since if they are we will set their amplitudes to zero (taking them out of the fit)
        # which we do by setting the array elements to zero
        bitCombFit = np.squeeze(np.asarray(a_tset < 0,dtype=int))
        
        # allow the DC template (the last background fit) and the 
        # slope (the second to last background) be any polarity
        bitCombFit[-1] = 1
        bitCombFit[-2] = 1
        #print('bitCombFit=',bitCombFit)
        #print('shape(bitCombFit)=', np.shape(bitCombFit))
        
        ######
        # gradient at Zero
        ######
        # this minus is because the vector to the origin is oposite the amplitude
        gradX2 = np.matmul(Wt_tset,(-a_tset))
        # this minus is because we want the negative gradient
        gradX2 = -gradX2
        # for the DC and slope component, automatically set the gradient to
        # a negative number we always want these fitted to be either pos or negative
        gradX2[-1]=-1
        gradX2[-2]=-1
        
        #record where the gradient is pointing
        # since if is positive we will set the amplitude to zero (taking them out of the fit)
        # which we do by setting the array elements to zero
        #bitCombFit = np.squeeze(np.asarray(gradX2 < 0,dtype=int))
        #print('bitCombFitGrad=',bitCombFit)
        #print('shape(bitCombFitGrad)=', np.shape(bitCombFitGrad))
        #######
        # end gradient at Zero
        #######
        
        Wt_tsetMask, iWt_tsetMask = of_nSmB_getWt(Wf_l, Wf_l_summed, Wt_l,nt,combInd=None,
                                        bindelay=ii,bitComb=bitComb,bitMask=bitCombFit)
        # select the elements that are not zero
        indexBitMask = np.squeeze(np.nonzero(bitCombFit))
        
        Pfiltt2_tsetMask = Pfiltt2_tset[np.ix_(indexBitMask)]
        #print('shape(Pfiltt2_tsetMask)=',np.shape(Pfiltt2_tsetMask))
        a_tsetMask = np.matmul(iWt_tsetMask,Pfiltt2_tsetMask)
        #print('shape(a_tsetMask)=',np.shape(a_tsetMask))
        #print('a_tsetMask=',a_tsetMask)
        
        # make a_tsetNew with dimentions of a_tset and populate it
        # with the a_tsetMask values
        a_tsetNewOneT = np.zeros((nSB,1))
        a_tsetNewOneT[np.ix_(indexBitMask)] = a_tsetMask
        #print('shape(a_tsetNewOneT)=',np.shape(a_tsetNewOneT))
        a_tsetNew[:,ii]=np.squeeze(a_tsetNewOneT)
        
        if (np.amax(a_tsetNew[0:-1,ii]) > 0.0):
            #print(f'entering second check of negatives.ii={ii}')
            #print(f'ii={ii}')
            #print('a_tsetNew[:,ii]=',a_tsetNew[:,ii])
            
            # record if the amplitudes are positive
            # since if they are we will set their amplitudes to zero (taking them out of the fit)
            # which we do by setting the array elements to zero
            bitCombFit = np.squeeze(np.asarray(a_tsetNew[:,ii] < 0,dtype=int))
            
            # allow the DC template (the last background fit) and the 
            # slope (the second to last background) be any polarity
            bitCombFit[-1] = 1
            bitCombFit[-2] = 1
            
            Wt_tsetMask, iWt_tsetMask = of_nSmB_getWt(Wf_l, Wf_l_summed, Wt_l,nt,combInd=None,
                                        bindelay=ii,bitComb=bitComb,bitMask=bitCombFit)
            # select the elements that are not zero
            indexBitMask = np.squeeze(np.nonzero(bitCombFit))

            Pfiltt2_tsetMask = Pfiltt2_tset[np.ix_(indexBitMask)]
            a_tsetMask = np.matmul(iWt_tsetMask,Pfiltt2_tsetMask)
            
            
            a_tsetNewOneT = np.zeros((nSB,1))
            a_tsetNewOneT[np.ix_(indexBitMask)] = a_tsetMask
            # reset the a_tsetNew vector
            a_tsetNew[:,ii]=np.squeeze(a_tsetNewOneT)
            
            
        # save the bitCombFit array
        bitCombFitVec[:,ii]= bitCombFit
        #if (np.amax(a_tsetNew[0:-2,ii]) > 0.0):
            #print('PROBLEM, second collapsing did not fix negatives')
            #print('a_tsetNew[:,ii]=',a_tsetNew[:,ii])

        
        # plotting checks
        '''
        if (np.amax(a_tsetNew[0:-1,ii]) > 0.0):
            print(f'ii={ii}')
            print('a_tset=',np.transpose(a_tset))
            print('a_tsetNew[:,ii]=',a_tsetNew[:,ii])
            lpFiltFreq = 30e3
            plotnSmBOFFit(pulset,omega,fs,ii*dt,a_tset,sbTemplatef,nS,nB,nt,psddnu,dt,
                          lpFiltFreq,lgcsaveplots=0,figPrefix=f'p1bin2805sFitIndex{ii}')
            plotnSmBOFFit(pulset,omega,fs,ii*dt,np.expand_dims(a_tsetNew[:,ii],1),sbTemplatef,nS,nB,nt,psddnu,dt,
                          lpFiltFreq,lgcsaveplots=0,figPrefix=f'p1bin2805sFitIndex{ii}')
        
            idxOfInt = 11
            reducedMat = iWt_tset[np.ix_([0,idxOfInt], [0,idxOfInt])]
            #print('np.shape(reducedMat)=', np.shape(reducedMat))
            #print('reducedMat = ', reducedMat)
            
            reducedHess =  Wt_tset[np.ix_([0,idxOfInt], [0,idxOfInt])]
            #print('reducedHess = ', reducedHess)
            
            gradX2red = np.matmul(reducedHess,np.array((-a_tset[0], -a_tset[idxOfInt])))
            ngradX2red = -gradX2red
            
            lambda_, v = np.linalg.eig(reducedMat)
            #print('lambda=', lambda_)
            lambda_ = np.sqrt(lambda_)
            #print('v=',v)
            theta = np.degrees(np.arctan2(*v[:,0][::-1]))
            
            fig = plt.figure(0)
            ax = fig.add_subplot(111, aspect='equal')
            
            nsigmas=3
            factorSig = 0.05
            for sigma in range(1,nsigmas+1):
                width = lambda_[0]*2*sigma*factorSig
                height = lambda_[1]*2*sigma*factorSig
                ell = Ellipse(xy=(a_tset[0], a_tset[idxOfInt]),
                                            width=width,
                                            height=height,
                                            angle=theta,
                                            linewidth=2,
                                            edgecolor=([0, 0, 1]),
                                           facecolor='none')
                ax.add_artist(ell)
                
            plt.plot(a_tset[0], a_tset[idxOfInt], '*r')
            #plt.plot(0,0,'+b', markersize=20)
            
            plotAx = np.max([width,height])/1.5
            plt.xlim([a_tset[0]-plotAx, a_tset[0]+plotAx])
            plt.ylim([a_tset[idxOfInt]-plotAx, a_tset[idxOfInt]+plotAx])
            plt.axvline(x=0.0, color='k',linestyle='--' )
            plt.axhline(y=0.0, color='k',linestyle='--')
            
            plt.grid()
            plt.ticklabel_format(style='sci',axis='both', scilimits=(0,0))
            plt.xlabel('signal amplitude')
            plt.ylabel('background amplitude')
            plt.title(f'$\chi^2$ with signal bin offset ={ii}')
            plt.quiver([0],[0],ngradX2red[0],ngradX2red[1],color='m',scale=1,scale_units='xy')

            
            if False:
                saveDir = '/galbascratch/wpage/analysis/samsNBs/Figures/'
                plt.savefig(saveDir + f"p1bin2805ellipse{ii}" + '.png', bbox_inches='tight')
            plt.show()
        '''
            
        
        # calc chi2 of positive constraint fit 
        chi2t0setMask = np.sum(a_tsetMask*Pfiltt2_tsetMask,0)

        chi2New[ii] = chi2base-chi2t0setMask
        
        # WARNING: the LF chie 
        chi2New_LF[ii] = chi2base_LF-chi2t0setMask
        
        tempLF1 = a_tsetMask
        tempLF2 = Pfiltt2_tsetMask

        #print('tempLF1=', np.shape(tempLF1))
        #print('tempLF2=', np.shape(tempLF2))


                
    chi2New = np.squeeze(chi2New)
    chi2minNew= np.amin(chi2New[ind_window])
    ind_tdel_smNew = np.argmin(chi2New[ind_window])
    ind_tdel_New=ind_window[:,ind_tdel_smNew]
    
    chi2minNew_LF = chi2New_LF[ind_tdel_New]
    
    aminNew = a_tsetNew[:,ind_tdel_New]
    tdelminNew = ((ind_tdel_New) - nt*(ind_tdel_New>nt/2 ))*dt
    bitCombFitFinal = np.squeeze(bitCombFitVec[:,ind_tdel_New])
    
    #print('chi2minNew=', chi2minNew)
    #print('chi2minNew_LF=', chi2minNew_LF)

    
    #print('ind_tdel_New=', ind_tdel_New)
    #print('aminNew=', aminNew)
    #print('tdelminNew=', tdelminNew)
    
    if lgcplotcheck:
        plt.figure(figsize=(12, 7))
        plt.plot(np.arange(nt), chi2New, '.b');
        plt.xlabel('bin offset')
        plt.ylabel('$\chi^2$ (new)')
        plt.grid(which='both')
        #plt.ylim([6417, 6500])
        
    ## check the gradient of the best fit positive only constraint
    
    ########
    # gradient at the new minimum
    #######

    # get the weighting matrix (again) at the bin with the lowest chi2
    # notice that we have to cast ind_tdel_New to an int
    #print('going into of_nSmB_getWt for gradient (line 1136))
    Wt_tmin, iWt_tmin = of_nSmB_getWt(Wf_l, Wf_l_summed, Wt_l,nt,combInd=((2**nSB)-1),
                                    bindelay=int(ind_tdel_New),bitComb=bitComb,bitMask=None)
    
    # change Pfiltt2 to (jtX2X1)
    Pfiltt2_tmin = Pfiltt2N[int(ind_tdel_New)]
    #print('shape(Pfiltt2_tmin)=',np.shape(Pfiltt2_tmin))
    a_tmin = np.matmul(iWt_tmin,Pfiltt2_tmin)
    
    # the vector that points from the absolute minimum to the new minimum
    vecFromAbsMin = aminNew - a_tmin
    #print('shape vecFromAbsMin', np.shape(vecFromAbsMin))

    # note that we are working the space where the absolute (unconstrained) min
    # is at the origin and vecFromAbsMin points to the constrained minimum
    gradX2aNew = np.matmul(Wt_tmin,vecFromAbsMin)    
    # this minus is because we want the negative gradient
    gradX2aNew = -gradX2aNew
    # for the final 2 dimensions, automatically set the gradient to
    # -99999
    gradX2aNew[-1]=-99999
    gradX2aNew[-2]=-99999
    #print('gradX2aNew=', np.transpose(gradX2aNew))

    #print('a_tmin=', np.transpose(a_tmin))

    #print('a_tsetNew=', np.transpose(aminNew))


    #print(np.concatenate((np.expand_dims(np.squeeze(gradX2aNew)<0,1),
    #                      np.expand_dims(np.squeeze(aminNew)<0,1))
    #                      ,axis=1))
    

    ###### ==== end test positive only constraint ====== ######

    #=== Subtract off the background for residual ===
    
    # get amplitudes of background templates
    aminb = amin[nS:nSB]
    
    # construct time domain total background best fit
    bestFitB = bTemplate@aminb  
    # make signal residual (pulse minus total background best fit)
    Pulset_BF= np.squeeze(pulset.T - bestFitB)
    # squeeze amin 
    aminsqueeze = np.squeeze(amin)
    
    aminsqueezeNew = np.squeeze(aminNew)
    if lgcplot:
        lpFiltFreq = 30e3
        '''
        plotnSmBOFFit(pulset,omega,fs,tdelmin,amin,sbTemplatef,nS,nB,nt,psddnu,dt,
                      lpFiltFreq,lgcsaveplots=lgcsaveplots,figPrefix='sFit')
        
        # plot the background only fit
        # need to set tdelmin to 0 because the first template bTemplatef is being interpreted
        # as the signal
        aminNoSig = np.insert(bOnlyA,0,0)
        aminNoSig = np.expand_dims(aminNoSig,1)
        
        #print('shape aminNoSig', np.shape(aminNoSig))
        #print('aminNoSig = ', aminNoSig)
        #print('amin=', amin)
            
        plotnSmBOFFit(pulset,omega,fs,0,aminNoSig,sbTemplatef,nS,nB,nt,psddnu,dt,
                      lpFiltFreq,lgcsaveplots=lgcsaveplots,figPrefix='bFit')
        '''
        
        # temporary: plot the original fit (no constraint) at the pos contraint delat
        #plotnSmBOFFit(pulset,omega,fs,tdelminNew,a_tmin,sbTemplatef,nS,nB,nt,psddnu,dt,
        #              lpFiltFreq,lgcsaveplots=lgcsaveplots,figPrefix='scFit')
        
        # plot the positive only constraint
        plotnSmBOFFit(pulset,omega,fs,tdelminNew,aminNew,sbTemplatef,nS,nB,nt,psddnu,dt,
                      lpFiltFreq,lgcsaveplots=lgcsaveplots,figPrefix='scFit',
                      background_templates_shifts = background_templates_shifts)
    
        # plot background only fit without constraint
        #aminNoSig = np.insert(bOnlyA,0,0)
        #aminNoSig = np.expand_dims(aminNoSig,1)
        #plotnSmBOFFit(pulset,omega,fs,0,aminNoSig,sbTemplatef,nS,nB,nt,psddnu,dt,
        #              lpFiltFreq,lgcsaveplots=lgcsaveplots,figPrefix='bFit')
        
    
        # plot background only fit with constraint
        aminNoSigCon = np.insert(bOnlyACon,0,0)
        aminNoSigCon = np.expand_dims(aminNoSigCon,1)
        plotnSmBOFFit(pulset,omega,fs,0,aminNoSigCon,sbTemplatef,nS,nB,nt,psddnu,dt,
                      lpFiltFreq,lgcsaveplots=lgcsaveplots,figPrefix='bcFit',
                      background_templates_shifts = background_templates_shifts)

    #return aminsqueeze,tdelmin,chi2min,Pulset_BF,a0,chi20, chi2BOnly
    return aminsqueezeNew, tdelminNew, bminsqueezeNew, chi2minNew, chi2minNew_LF, Pulset_BF,a0,chi20, chi2BOnlyCon, chi2BOnlyCon_LF

def get_slope_dc_template_nsmb(nbin):
    """
    Function for constructing the background templates for the OF nsmb fit when
    the backgrounds to be fitted are just a slope and dc component. These background
    templates could fit the effects of muons, for example, so would be useful to use
    in surface detector testing
    
    Parameters
    ----------
    nbin : int
        The number of bins to make the template
        
    Returns
    -------
    backgroundtemplates : ndarray
        The time domain background templates
    backgroundtemplateshifts : ndarray
        The time domain bin shifts of the template, which is an optional
        parameter for the OF nsmb. Slope and dc templates have no offsets,
        so these are set to nan
    """
    
    # construct the background templates
    backgroundtemplates = np.ones((nbin,2))
    
    # construct the sloped background
    backgroundtemplates[:,-2] = np.arange(0,1,1/nbin)
    
    backgroundtemplatesshifts = np.empty((2))
    backgroundtemplatesshifts[:] = np.nan
    
    return backgroundtemplates, backgroundtemplatesshifts
    
    
def maketemplate_ttlfit_nsmb(template, fs, ttlrate, lgcconstrainpolarity=False, lgcpositivepolarity=True):
    """
    Function for constructing the background templates for the OF nsmb fit when
    the backgrounds to be fitted are pulses from an periodic laser (TTL) firing.
    A slope and dc component are also included in the background templates. 
    
    Parameters
    ----------
    template : ndarray
        The time domain background template with the pulse centered
    fs : float
        The sample rate of the data being taken (in Hz).
    ttlrate : float
        The rate of the ttl pulses
    lgcconstrainpolarity : bool
        Boolean flag for whether in the OF fit to constrain
        the ttl amplitudes to be a certain polarity. Default
        is False
    lgcpositivepolarity : bool
        Boolean flag for whether the the polarity of the
        pulses are positive or negative. Used to set the 
        returned array backgroundpolarityconstraint
    Returns
    -------
    backgroundtemplates : ndarray
        The time domain background templates
    backgroundtemplateshifts : ndarray
        The time domain bin shifts of the template, which is an optional
        parameter for the OF nsmb. Slope and dc templates have no offsets,
        so these are set to nan
    backgroundpolarityconstraint : ndarray
        The array to tell the OF fit whether or not to constrain the polarity
        of the amplitude.
            If 0, then no constraint on the pulse direction is set
            If 1, then a positive pulse constraint is set.
            If -1, then a negative pulse constraint is set.
    """
    
    nbin = len(template)

    # time between ttl triggers
    ttltimeBetween = 1/ttlrate

    # bins between ttl triggers
    ttlbinsBetween = ttltimeBetween*fs

    # how many ttl triggers in the trace
    tLengthTrace = nbin/fs
    
    nTTLs = int(tLengthTrace/ttltimeBetween)

    # shift the template back by nTTLs/2*ttlbinsBetween bins
    #
    binShift = int(-nTTLs/2*ttlbinsBetween)
    firsttemplate = np.roll(template,binShift)
    
    # create matrix of background templates
    backgroundtemplates = np.ones((nbin,nTTLs+2))
    # record of bin shifts
    backgroundtemplateshifts = np.zeros((nTTLs+2))
    
    # set polarity constrain for the OF fit
    backgroundpolarityconstraint = np.zeros((nTTLs+2))

    #circularly shift template in time for background templates
    for ii in range(0,nTTLs):
        backgroundtemplateshift = int(np.rint(ttlbinsBetween*ii))
        backgroundtemplateshifts[ii] = backgroundtemplateshift
        if (ii==0):
            backgroundtemplates[:,ii] = firsttemplate
        else:
            backgroundtemplates[:,ii] = np.pad(firsttemplate,(backgroundtemplateshift,0), mode='constant')[:-backgroundtemplateshift]
        
        if lgcconstrainpolarity:
            if lgcpositivepolarity:
                backgroundpolarityconstraint[ii] = 1
            else:
                backgroundpolarityconstraint[ii] = -1
            
    # construct the sloped background for the second to last template
    backgroundtemplates[:,-2] = np.arange(0,1,1/nbin)

    # the slope and dc background don't have a bin shift,
    # so set these values to nan
    backgroundtemplateshifts[-1] = np.nan
    backgroundtemplateshifts[-2] = np.nan
    
    return backgroundtemplates, backgroundtemplateshifts, backgroundpolarityconstraint
    
    

def chi2lowfreq(signal, template, amp, t0, inputpsd, fs, fcutoff=10000, coupling="AC"):
    """
    Function for calculating the low frequency chi^2 of the optimum filter, given some cut off 
    frequency. This function does not calculate the optimum amplitude - it requires that ofamp
    has been run, and the fit has been loaded to this function.
    
    Parameters
    ----------
    signal : ndarray
        The signal that we want to calculate the low frequency chi^2 of (units should be Amps).
    template : ndarray
        The pulse template to be used for the low frequency chi^2 calculation (should be 
        normalized beforehand).
    amp : float
        The optimum amplitude calculated for the trace (in Amps).
    t0 : float
        The time shift calculated for the pulse (in s).
    inputpsd : ndarray
        The two-sided psd that will be used to describe the noise in the signal (in Amps^2/Hz).
    fs : float
        The sample rate of the data being taken (in Hz).
    fcutoff : float, optional
        The frequency (in Hz) that we should cut off the chi^2 when calculating the low frequency chi^2.
    coupling : str, optional
        String that determines if the zero frequency bin of the psd should be ignored (i.e. set to infinity)
        when calculating the optimum amplitude. If set to 'AC', then ths zero frequency bin is ignored. If
        set to anything else, then the zero frequency bin is kept. Default is 'AC'.
        
    Returns
    -------
    chi2low : float
        The low frequency chi^2 value (cut off at fcutoff) for the inputted values.
        
    """
    
    psd = np.zeros(len(inputpsd))
    psd[:] = inputpsd
    
    if coupling=="AC":
        psd[0] = np.inf
    
    if len(signal.shape)==1:
        signal = signal[np.newaxis, :]
        
    if np.isscalar(amp):
        amp = np.array([amp])
        t0 = np.array([t0])
    
    nbins = signal.shape[-1]
    df = fs/nbins
    
    v = fft(signal, axis=-1)/nbins/df
    s = fft(template)/nbins/df
    
    f = fftfreq(nbins, d=1/fs)
    
    chi2tot = df*np.abs(v-amp[:, np.newaxis]*np.exp(-2.0j*np.pi*t0[:, np.newaxis]*f[np.newaxis, :])*s)**2/psd
    
    chi2inds = np.abs(f)<=fcutoff
    
    chi2low = np.sum(chi2tot[:, chi2inds], axis=-1)
    
    if len(chi2low)==1:
        chi2low = chi2low[0]
    
    return chi2low

def chi2_nopulse(signal, inputpsd, fs, coupling="AC"):
    """
    Function for calculating the chi^2 of a trace with the assumption that there is no pulse.
    
    Parameters
    ----------
    signal : ndarray
        The signal that we want to calculate the no pulse chi^2 of (units should be Amps).
    inputpsd : ndarray
        The two-sided psd that will be used to describe the noise in the signal (in Amps^2/Hz).
    fs : float
        The sample rate of the data being taken (in Hz).
    coupling : str, optional
        String that determines if the zero frequency bin of the psd should be ignored (i.e. set to infinity)
        when calculating the no pulse chi^2 . If set to 'AC', then the zero frequency bin is ignored. If
        set to anything else, then the zero frequency bin is kept. Default is 'AC'.
        
    Returns
    -------
    chi2_0 : float
        The chi^2 value for there being no pulse.
        
    """
    
    psd = np.zeros(len(inputpsd))
    psd[:] = inputpsd
    
    nbins = signal.shape[-1]
    df = fs/nbins
    
    v = fft(signal, axis=-1)/nbins/df
    
    if coupling == 'AC':
        psd[0]=np.inf
    
    chi2_0 = df*np.sum(np.abs(v)**2/psd, axis=-1)
    
    return chi2_0


class OFnonlin(object):
    """
    This class provides the user with a non-linear optimum filter to estimate the amplitude,
    rise time (optional), fall time, and time offset of a pulse. 
    
    Attributes:
    -----------
    psd : ndarray 
        The power spectral density corresponding to the pulses that will be 
        used in the fit. Must be the full psd (positive and negative frequencies), 
        and should be properly normalized to whatever units the pulses will be in. 
    fs : int or float
        The sample rate of the ADC
    df : float
        The delta frequency
    freqs : ndarray
        Array of frequencies corresponding to the psd
    time : ndarray
        Array of time bins corresponding to the pulse
    template : ndarray
        The time series pulse template to use as a guess for initial parameters
    data : ndarray
        FFT of the pulse that will be used in the fit
    lgcdouble : bool
        If False, only the Pulse hight, fall time, and time offset will be fit.
        If True, the rise time of the pulse will be fit in addition to the above. 
    taurise : float
        The user defined risetime of the pulse
    error : ndarray
        The uncertianty per frequency (the square root of the psd, devided by the errorscale)
    dof : int
        The number of degrees of freedom in the fit
    norm : float
        Normalization factor to go from continuous to FFT
    
    """
    
    def __init__(self, psd, fs, template = None):
        """
        Initialization of OFnonlin object
        
        Parameters
        ----------
        psd : ndarray 
            The power spectral density corresponding to the pulses that will be 
            used in the fit. Must be the full psd (positive and negative frequencies), 
            and should be properly normalized to whatever units the pulses will be in. 
        fs : int or float
            The sample rate of the ADC
        template : ndarray
            The time series pulse template to use as a guess for initial parameters
            
        """
        
        self.psd = np.zeros(len(psd))
        self.psd[:] = psd
        self.psd[0] = 1e40
        
        self.fs = fs
        self.df = fs/len(psd)
        self.freqs = np.fft.fftfreq(len(psd), 1/fs)
        self.time = np.arange(len(psd))/fs
        self.template = template
        
        self.data = None
        self.npolefit = 1
        
        self.taurise = None
        self.error = None
        self.dof = None
        self.norm = np.sqrt(fs*len(psd))
        
    
    def fourpole(self, A, B, C, tau_r, tau_f1, tau_f2, tau_f3, t0):
        """
        Functional form of pulse in frequency domain with 1 rise time and three fall times
        The fall times have independent amplitudes (A,B,C). The condition f(0)=0 requires
        the rise time to have amplitude (A+B+C). Therefore, the "amplitudes" take on different
        meanings than in other n-pole functions. The functional form (time-domain) is:
        A*(exp(-t/tau_fall1)) + B*(exp(-t/tau_fall2)) + 
        C*(exp(-t/tau_fall3)) - (A+B+C)*(exp(-t/tau_rise))
        
        4 rise/fall times, 3 amplitudes, and time offset allowed to float
        
        
        Parameters
        ----------
        A : float
            Amplitude for first fall time
        B : float
            Amplitude for second fall time
        C : float
            Amplitude for third fall time
        tau_r : float
            Rise time of pulse
        tau_f1 : float
            First fall time of pulse
        tau_f2 : float
            Second fall time of pulse
        tau_f3 : float
            Third fall time of pulse
        t0 : float
            Time offset of four pole pulse
               
        Returns
        -------
        pulse : ndarray, complex
            Array of amplitude values as a function of frequency
            
        """
        omega = 2*np.pi*self.freqs
        phaseTDelay = np.exp(-(0+1j)*omega*t0)
        pulse = (A*(tau_f1/(1+omega*tau_f1*(0+1j))) + B*(tau_f2/(1+omega*tau_f2*(0+1j))) + \
        C*(tau_f3/(1+omega*tau_f3*(0+1j))) - (A+B+C)*(tau_r/(1+omega*tau_r*(0+1j)))) * phaseTDelay
        return pulse*np.sqrt(self.df)
        
    def fourpoletime(self, A, B, C, tau_r, tau_f1, tau_f2, tau_f3, t0):
        """
        Functional form of pulse in time domain with 1 rise time and three fall times
        The fall times have independent amplitudes (A,B,C). The condition f(0)=0 requires
        the rise time to have amplitude (A+B+C). Therefore, the "amplitudes" take on different
        meanings than in other n-pole functions. The functional form (time-domain) is:
        A*(exp(-t/tau_fall1)) + B*(exp(-t/tau_fall2)) + 
        C*(exp(-t/tau_fall3)) - (A+B+C)*(exp(-t/tau_rise))
        
        4 rise/fall times, 3 amplitudes, and time offset allowed to float
        
        
        Parameters
        ----------
        A : float
            Amplitude for first fall time
        B : float
            Amplitude for second fall time
        C : float
            Amplitude for third fall time
        tau_r : float
            Rise time of pulse
        tau_f1 : float
            First fall time of pulse
        tau_f2 : float
            Second fall time of pulse
        tau_f3 : float
            Third fall time of pulse
        t0 : float
            Time offset of four pole pulse
            
        Returns
        -------
        pulse : ndarray
            Array of amplitude values as a function of time
        """
        
        pulse = A*(np.exp(-self.time/tau_f1)) + \
        B*(np.exp(-self.time/tau_f2)) + \
        C*(np.exp(-self.time/tau_f3)) - \
        (A+B+C)*(np.exp(-self.time/tau_r))
        return np.roll(pulse, int(t0*self.fs))       
    
    def threepole(self, A, B, tau_r, tau_f1, tau_f2, t0):
        """
        Functional form of pulse in frequency domain with 1 rise time and two fall times
        The  fall times have independent amplitudes (A,B) and the condition f(0)=0 constrains 
        the rise time to have amplitude (A+B). The functional form (time domain) is:
        A*(exp(-t/\tau_fall1)) + B*(exp(-t/\tau_fall2)) - (A+B)*(exp(-t/\tau_rise)) and therefore
        the "amplitudes" take on different meanings than in the other n-pole functions
        
        3 rise/fall times, 2 amplitudes, and time offset allowed to float
        
        Parameters
        ----------
        A : float
            Amplitude for first fall time
        B : float
            Amplitude for second fall time
        tau_r : float
            Rise time of pulse
        tau_f1 : float
            First fall time of pulse
        tau_f2 : float
            Second fall time of pulse
        t0 : float
            Time offset of three pole pulse
                
        Returns
        -------
        pulse : ndarray, complex
            Array of amplitude values as a function of frequency
            
        """
        omega = 2*np.pi*self.freqs
        phaseTDelay = np.exp(-(0+1j)*omega*t0)
        pulse = (A*(tau_f1/(1+omega*tau_f1*(0+1j))) + B*(tau_f2/(1+omega*tau_f2*(0+1j))) - \
        (A+B)*(tau_r/(1+omega*tau_r*(0+1j)))) * phaseTDelay
        return pulse*np.sqrt(self.df)
        
    
    def threepoletime(self, A, B, tau_r, tau_f1, tau_f2, t0):
        """
        Functional form of pulse in time domain with 1 rise time and two fall times
        The  fall times have independent amplitudes (A,B) and the condition f(0)=0 constrains 
        the rise time to have amplitude (A+B). The functional form (time domain) is:
        A*(exp(-t/\tau_fall1)) + B*(exp(-t/\tau_fall2)) - (A+B)*(exp(-t/\tau_rise)) and therefore
        the "amplitudes" take on different meanings than in the other n-pole functions
        
        3 rise/fall times, 2 amplitudes, and time offset allowed to float
        
        Parameters
        ----------
        A : float
            Amplitude for first fall time
        B : float
            Amplitude for second fall time
        tau_r : float
            Rise time of pulse
        tau_f1 : float
            First fall time of pulse
        tau_f2 : float
            Second fall time of pulse
        t0 : float
            Time offset of three pole pulse
        
        Returns
        -------
        pulse : ndarray
            Array of amplitude values as a function of time
        """

        
        pulse = A*(np.exp(-self.time/tau_f1)) + B*(np.exp(-self.time/tau_f2)) - \
        (A+B)*(np.exp(-self.time/tau_r))
        return np.roll(pulse, int(t0*self.fs))  
        
    
    def twopole(self, A, tau_r, tau_f,t0):
        """
        Functional form of pulse in frequency domain with the amplitude, rise time,
        fall time, and time offset allowed to float. This is meant to be a private function
        
        Parameters
        ----------
        A : float
            Amplitude of pulse
        tau_r : float
            Rise time of two-pole pulse
        tau_f : float
            Fall time of two-pole pulse
        t0 : float
            Time offset of two pole pulse
                
        Returns
        -------
        pulse : ndarray, complex
            Array of amplitude values as a function of frequency
            
        """
        
        omega = 2*np.pi*self.freqs
        delta = tau_r-tau_f
        rat = tau_r/tau_f
        amp = A/(rat**(-tau_r/delta)-rat**(-tau_f/delta))
        pulse = amp*np.abs(tau_r-tau_f)/(1+omega*tau_f*1j)*1/(1+omega*tau_r*1j)*np.exp(-omega*t0*1.0j)
        return pulse*np.sqrt(self.df)
    
    def twopoletime(self, A, tau_r, tau_f, t0):
        """
        Functional form of pulse in time domain with the amplitude, rise time,
        fall time, and time offset allowed to float 
        
        Parameters
        ----------
        A : float
            Amplitude of pulse
        tau_r : float
            Rise time of two-pole pulse
        tau_f : float
            Fall time of two-pole pulse
        t0 : float
            Time offset of two pole pulse
                
        Returns
        -------
        pulse : ndarray
            Array of amplitude values as a function of time
        """
        
        delta = tau_r-tau_f
        rat = tau_r/tau_f
        amp = A/(rat**(-tau_r/delta)-rat**(-tau_f/delta))
        pulse = amp*(np.exp(-(self.time)/tau_f)-np.exp(-(self.time)/tau_r))
        return np.roll(pulse, int(t0*self.fs))

    def onepole(self, A, tau_f, t0):
        """
        Functional form of pulse in time domain with the amplitude,
        fall time, and time offset allowed to float, and the rise time 
        held constant
        
        Parameters
        ----------
        A : float
            Amplitude of pulse
        tau_f : float
            Fall time of two-pole pulse
        t0 : float
            Time offset of two pole pulse
                
        Returns
        -------
        pulse : ndarray, complex
            Array of amplitude values as a function of freuqncy
        """
        
        tau_r = self.taurise
        return self.twopole(A, tau_r,tau_f,t0)

    def residuals(self, params):
        """
        Function ot calculate the weighted residuals to be minimized
        
        Parameters
        ----------
        params : tuple
            Tuple containing fit parameters
                
        Returns
        -------
        z1d : ndarray
            Array containing residuals per frequency bin. The complex data is flatted into
            single array
        """
        
        if (self.npolefit==4):
            A, B, C, tau_r, tau_f1, tau_f2, tau_f3, t0 = params
            delta = (self.data - self.fourpole( A, B, C, tau_r, tau_f1, tau_f2, tau_f3, t0) )
        elif (self.npolefit==3):
            A, B, tau_r, tau_f1, tau_f2, t0 = params
            delta = (self.data - self.threepole( A, B, tau_r, tau_f1, tau_f2, t0) )
        elif (self.npolefit==2):
            A,tau_r, tau_f, t0 = params
            delta = (self.data - self.twopole( A, tau_r, tau_f, t0) )
        else:
            A, tau_f, t0 = params
            delta = (self.data - self.onepole( A,  tau_f, t0) )
        z1d = np.zeros(self.data.size*2, dtype = np.float64)
        z1d[0:z1d.size:2] = delta.real/self.error
        z1d[1:z1d.size:2] = delta.imag/self.error
        return z1d
    
    
    def calcchi2(self, model):
        """
        Function to calculate the reduced chi square
        
        Parameters
        ----------
        model : ndarray
            Array corresponding to pulse function (twopole or onepole) evaluated
            at the optimum values
                
        Returns
        -------
        chi2 : float
            The reduced chi squared statistic
        """
        
        return sum(np.abs(self.data-model)**2/self.error**2)/(len(self.data)-self.dof)

    def fit_falltimes(self, pulse, npolefit=1, errscale=1, guess=None, taurise=None, 
                      lgcfullrtn=False, lgcplot=False):
        """
        Function to do the fit
        
        Parameters
        ----------
        pulse : ndarray
            Time series traces to be fit
        npolefit: int, optional
            The number of poles to fit
            If 1, the one pole fit is done, the user must provide the value of taurise
            If 2, the two pole fit is done
            If 3, the three pole fit is done (1 rise 2 fall). Second fall time amplitude is independent
            If 4, the four pole fit is done (1 rise 3 fall). Second and third fall time amplitudes are independent
            If False, the twopole fit is done, if True, the one pole fit it done.
        errscale : float or int, optional
            A scale factor for the psd. Ex: if fitting an average, the errscale should be
            set to the number of traces used in the average
        guess : tuple, optional
            Guess of initial values for fit, must be the same size as the model being used for fit.
            If lgcdouble is True, then the order should be (ampguess, tauriseguess, taufallguess, t0guess).
            If lgcdouble is False, then the order should be (ampguess, taufallguess, t0guess).
        taurise : float, optional
            The value of the rise time of the pulse if the single pole function is being use for fit
        lgcfullrtn : bool, optional
            If False, only the best fit parameters are returned. If True, the errors in the fit parameters,
            the covariance matrix, and chi squared statistic are returned as well.
        lgcplot : bool, optional
            If True, diagnostic plots are returned. 
                
        Returns
        -------
        variables : tuple
            The best fit parameters
        errors : tuple
            The corresponding fit errors for the best fit parameters
        cov : ndarray
            The convariance matrix returned from the fit
        chi2 : float
            The reduced chi squared statistic evaluated at the optimum point of the fit
                
        Raises
        ------
        ValueError
            if length of guess does not match the number of parameters needed in fit
                
        """
        
        self.data = np.fft.fft(pulse)/self.norm
        self.error = np.sqrt(self.psd/errscale)
        
        self.npolefit = npolefit
        
        if (self.npolefit==1):
            if taurise is None:
                raise ValueError('taurise must not be None if doing 1-pole fit.')
            else:
                self.taurise = taurise
        
        if guess is not None:
            if (self.npolefit==4):
                if len(guess) != 8:
                    raise ValueError(f'Length of guess not compatible with 4-pole fit. Must be of format: guess = (A,B,C,taurise,taufall1,taufall2,taufall3,t0)')
                else:
                    Aguess, Bguess, Cguess, tauriseguess, taufall1guess, taufall2guess, taufall3guess, t0guess = guess
            elif (self.npolefit==3):
                if len(guess) != 6:
                    raise ValueError(f'Length of guess not compatible with 3-pole fit. Must be of format: guess = (A,B,taurise,taufall1,taufall2,t0)')
                else:
                    Aguess, Bguess, tauriseguess, taufall1guess, taufall2guess, t0guess = guess
            elif (self.npolefit==2):
                if len(guess) != 4:
                    raise ValueError(f'Length of guess not compatible with 2-pole fit. Must be of format: guess = (A,taurise,taufall,t0)')
                else:
                    ampguess, tauriseguess, taufallguess, t0guess = guess
            else:
                if len(guess) != 3:
                    raise ValueError(f'Length of guess not compatible with 1-pole fit. Must be of format: guess = (A,taufall,t0)')
                else:
                    ampguess, taufallguess, t0guess = guess
        else:
            # before making guesses, if self.template
            # has been defined then define maxind, 
            # ampscale, and amplitudes using the template.
            # otherwise use the pulse
            if self.template is not None:
                ampscale = np.max(pulse)-np.min(pulse)
                templateforguess = self.template
            else:
                ampscale = 1
                templateforguess = pulse
                
            maxind = np.argmax(templateforguess)

            if (self.npolefit==4):
                # guesses need to be tuned depending
                # on the detector being analyzed.
                # good guess for t0 particularly important to provide
                Aguess = np.mean(templateforguess[maxind-7:maxind+7])*ampscale
                Bguess = Aguess/3
                Cguess = Aguess/3
                tauriseguess = 20e-6
                taufall1guess = 100e-6
                taufall2guess = 300e-6
                taufall3guess = 500e-6
                t0guess = maxind/self.fs
            elif (self.npolefit==3):
                Aguess = np.mean(templateforguess[maxind-7:maxind+7])*ampscale
                Bguess = Aguess/3
                tauriseguess = 20e-6
                taufall1guess = 100e-6
                taufall2guess = 300e-6
                t0guess = maxind/self.fs
            else:
                ampguess = np.mean(templateforguess[maxind-7:maxind+7])*ampscale
                tauval = 0.37*ampguess
                tauind = np.argmin(np.abs(pulse[maxind+1:maxind+1+int(300e-6*self.fs)]-tauval)) + maxind+1
                taufallguess = (tauind-maxind)/self.fs
                tauriseguess = 20e-6
                t0guess = maxind/self.fs
        
        
        if (self.npolefit==4):
            self.dof = 8
            p0 = (Aguess, Bguess, Cguess, tauriseguess, taufall1guess, taufall2guess, taufall3guess, t0guess)
            boundslower = (Aguess/100, Bguess/100, Cguess/100, tauriseguess/10, taufall1guess/10, taufall2guess/10, taufall3guess/10, t0guess - 30/self.fs)
            boundsupper = (Aguess*100, Bguess*100, Cguess*100, tauriseguess*10, taufall1guess*10, taufall2guess*10, taufall3guess*10, t0guess + 30/self.fs)
        elif (self.npolefit==3):
            self.dof = 6
            p0 = (Aguess, Bguess, tauriseguess, taufall1guess, taufall2guess, t0guess)
            boundslower = (Aguess/100, Bguess/100, tauriseguess/10, taufall1guess/10, taufall2guess/10, t0guess - 30/self.fs)
            boundsupper = (Aguess*100, Bguess*100, tauriseguess*10, taufall1guess*10, taufall2guess*10, t0guess + 30/self.fs)
        elif (self.npolefit==2):
            self.dof = 4
            p0 = (ampguess, tauriseguess, taufallguess, t0guess)
            boundslower = (ampguess/100, tauriseguess/10, taufallguess/10, t0guess - 30/self.fs)
            boundsupper = (ampguess*100, tauriseguess*10, taufallguess*10, t0guess + 30/self.fs)
        else:
            self.dof = 3
            p0 = (ampguess, taufallguess, t0guess)
            boundslower = (ampguess/100, taufallguess/10, t0guess - 30/self.fs)
            boundsupper = (ampguess*100,  taufallguess*10, t0guess + 30/self.fs)
        
        bounds = (boundslower, boundsupper)
        
        result = least_squares(self.residuals, x0 = p0, bounds=bounds, x_scale=p0 , jac = '3-point',
                               loss = 'linear', xtol = 2.3e-16, ftol = 2.3e-16)
        variables = result['x']
        

        if (self.npolefit==4):        
            chi2 = self.calcchi2(self.fourpole(variables[0], variables[1], 
                                                variables[2],variables[3], 
                                                variables[4], variables[5],
                                               variables[6], variables[7]))
        elif (self.npolefit==3):        
            chi2 = self.calcchi2(self.threepole(variables[0], variables[1], variables[2],variables[3], variables[4], variables[5]))
        elif (self.npolefit==2):        
            chi2 = self.calcchi2(self.twopole(variables[0], variables[1], variables[2],variables[3]))
        else:
            chi2 = self.calcchi2(self.onepole(variables[0], variables[1],variables[2]))
    
        jac = result['jac']
        cov = np.linalg.pinv(np.dot(np.transpose(jac),jac))
        errors = np.sqrt(cov.diagonal())
        
        if lgcplot:
            plotnonlin(self,pulse, variables, errors)
        if lgcfullrtn:
            return variables, errors, cov, chi2
        else:
            return variables


class MuonTailFit(object):
    """
    This class provides the user with a fitting routine to estimate the thermal muon tail fall time.
    
    Attributes:
    -----------
    psd : ndarray 
        The power spectral density corresponding to the pulses that will be 
        used in the fit. Must be the full psd (positive and negative frequencies), 
        and should be properly normalized to whatever units the pulses will be in. 
    fs : int or float
        The sample rate of the ADC
    df : float
        The delta frequency
    freqs : ndarray
        Array of frequencies corresponding to the psd
    time : ndarray
        Array of time bins corresponding to the pulse
    data : ndarray
        FFT of the pulse that will be used in the fit
    error : ndarray
        The uncertainty per frequency (the square root of the psd, divided by the error scale)
    dof : int
        The number of degrees of freedom in the fit
    norm : float
        Normalization factor to go from continuous to FFT
    
    """
    
    def __init__(self, psd, fs):
        """
        Initialization of MuonTailFit object
        
        Parameters
        ----------
        psd : ndarray 
            The power spectral density corresponding to the pulses that will be 
            used in the fit. Must be the full psd (positive and negative frequencies), 
            and should be properly normalized to whatever units the pulses will be in. 
        fs : int or float
            The sample rate of the ADC
            
        """
        
        self.psd = np.zeros(len(psd))
        self.psd[:] = psd
#         self.psd[0] = 1e40
        
        self.fs = fs
        self.df = self.fs/len(self.psd)
        self.freqs = np.fft.fftfreq(len(psd), d=1/self.fs)
        self.time = np.arange(len(self.psd))/self.fs

        self.data = None

        self.taurise = None
        self.error = None
        self.dof = None
        self.norm = np.sqrt(self.fs*len(self.psd))
        
        
    def muontailfcn(self, A, tau):
        """
        Functional form of a thermal muon tail in time domain with the amplitude and fall time
        allowed to float.
        
        Parameters
        ----------
        A : float
            Amplitude of pulse
        tau : float
            Fall time of muon tail
                
        Returns
        -------
        pulse : ndarray
            Array of amplitude values as a function of time
        """
        
        omega = 2*np.pi*self.freqs
        pulse = A*tau/(1+omega*tau*1j)
        return pulse*np.sqrt(self.df)
    
    def residuals(self, params):
        """
        Function to calculate the weighted residuals to be minimized.
        
        Parameters
        ----------
        params : tuple
            Tuple containing fit parameters
                
        Returns
        -------
        z1d : ndarray
            Array containing residuals per frequency bin. The complex data is flatted into
            single array.
        """
        
        A, tau = params
        delta = self.data - self.muontailfcn(A, tau)
        z1d = np.zeros(self.data.size*2, dtype = np.float64)
        z1d[0:z1d.size:2] = delta.real/self.error
        z1d[1:z1d.size:2] = delta.imag/self.error
        return z1d
    
    def calcchi2(self, model):
        """
        Function to calculate the chi square
        
        Parameters
        ----------
        model : ndarray
            Array corresponding to pulse function evaluated at the fitted values
                
        Returns
        -------
        chi2 : float
            The chi squared statistic
        """
        
        return np.sum(np.abs(self.data-model)**2/self.error**2)

    def fitmuontail(self, signal, lgcfullrtn=False, errscale=1):
        """
        Function to do the fit
        
        Parameters
        ----------
        signal: ndarray
            Time series traces to be fit
        lgcfullrtn : bool, optional
            If False, only the best fit parameters are returned. If True, the errors in the fit parameters,
            the covariance matrix, and chi squared statistic are returned as well.
        errscale : float or int, optional
            A scale factor for the psd. Ex: if fitting an average, the errscale should be
            set to the number of traces used in the average

        Returns
        -------
        variables : tuple
            The best fit parameters
        errors : tuple
            The corresponding fit errors for the best fit parameters
        cov : ndarray
            The convariance matrix returned from the fit
        chi2 : float
            The chi squared statistic evaluated at the fit
        """

        self.data = np.fft.fft(signal)/self.norm
        self.error = np.sqrt(self.psd/errscale)

        ampguess = np.max(signal) - np.min(signal)
        tauguess = np.argmin(np.abs(signal-ampguess/np.e))/self.fs

        p0 = (ampguess, tauguess)
        boundslower = (0, 0)
        boundsupper = (ampguess*100,  tauguess*100)
        bounds = (boundslower, boundsupper)

        result = least_squares(self.residuals, x0 = p0, bounds=bounds, x_scale=np.abs(p0) , jac = '3-point',
                               loss = 'linear', xtol = 2.3e-16, ftol = 2.3e-16)
        variables = result['x']
        chi2 = self.calcchi2(self.muontailfcn(*variables))

        jac = result['jac']
        cov = np.linalg.pinv(np.dot(np.transpose(jac),jac))
        errors = np.sqrt(cov.diagonal())

        if lgcfullrtn:
            return variables, errors, cov, chi2
        else:
            return variables
    
