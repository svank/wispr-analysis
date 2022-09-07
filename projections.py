from astropy.wcs import WCS
import matplotlib.pyplot as plt
import numpy as np
import reproject
import scipy.optimize

from . import utils


class RadialTransformer():
    def __init__(self, ref_pa, ref_y, dpa,
            ref_elongation, ref_x, delongation, wcs_in,
            pa_of_ecliptic=90):
        self.ref_pa = ref_pa
        self.ref_elongation = ref_elongation
        self.ref_x = ref_x
        self.ref_y = ref_y
        self.dpa = dpa
        self.delongation = delongation
        self.wcs_in = wcs_in
        
        self.pa_of_ecliptic = pa_of_ecliptic
    
    
    def __call__(self, pixel_out):
        pixel_out = np.asarray(pixel_out, dtype=float)
        pixel_in = np.empty_like(pixel_out)
        elongation, pa = self.all_pix2world(
                pixel_out[..., 0], pixel_out[..., 1], 0)
        
        hp_lon, hp_lat = self.elongation_to_hp(elongation, pa)
        
        input_x, input_y = self.wcs_in.all_world2pix(hp_lon, hp_lat, 0)
        
        pixel_in[..., 0] = input_x
        pixel_in[..., 1] = input_y
        return pixel_in
    
    
    def hp_to_elongation(self, lon, lat):
        lon = np.asarray(lon) * np.pi / 180
        lat = np.asarray(lat) * np.pi / 180
        
        # Expressions from Snyder (1987)
        # https://pubs.er.usgs.gov/publication/pp1395
        # Eqn (5-3a)
        elongation = 2 * np.arcsin(np.sqrt(
            np.sin(lat/2)**2 + np.cos(lat) * np.sin(lon/2)**2
        ))
        # Eqn (5-4b)
        pa = np.arctan2(np.cos(lat) * np.sin(lon), np.sin(lat))
        
        elongation *= 180 / np.pi
        pa *= 180 / np.pi
        pa += (self.pa_of_ecliptic - 90)
        
        return elongation, pa
    
    
    def elongation_to_hp(self, elongation, pa):
        elongation = np.asarray(elongation) * np.pi / 180
        pa = np.asarray(pa) - (self.pa_of_ecliptic - 90)
        pa *= np.pi / 180
        
        # Expressions from Snyder (1987)
        # https://pubs.er.usgs.gov/publication/pp1395
        # Eqn (5-5)
        lat = np.arcsin(np.sin(elongation) * np.cos(pa))
        # Eqn (5-6)
        lon = np.arctan2(np.sin(elongation) * np.sin(pa), np.cos(elongation))
        
        lat *= 180 / np.pi
        lon *= 180 / np.pi
        return lon, lat
    
    
    def all_pix2world(self, x, y, origin=0):
        x = np.asarray(x) - origin
        y = np.asarray(y) - origin
        pa = (y - self.ref_y) * self.dpa + self.ref_pa
        elongation = ((x - self.ref_x) * self.delongation
                + self.ref_elongation)
        
        return elongation, pa
    
    
    def all_world2pix(self, elongation, pa, origin=0):
        elongation = np.asarray(elongation)
        pa = np.asarray(pa)
        x = (elongation - self.ref_elongation) / self.delongation + self.ref_x
        y = (pa - self.ref_pa) / self.dpa + self.ref_y
        
        x += origin
        y += origin
        
        return x, y


class InverseRadialTransformer(RadialTransformer):
    def __call__(self, pixel_out):
        pixel_out = np.asarray(pixel_out, dtype=float)
        pixel_in = np.empty_like(pixel_out)
        hp_lon, hp_lat = self.wcs_in.all_pix2world(
                pixel_out[..., 0], pixel_out[..., 1], 0)
        
        elongation, pa = self.hp_to_elongation(hp_lon, hp_lat)
        
        input_x, input_y = self.all_world2pix(elongation, pa, 0)
        
        pixel_in[..., 0] = input_x
        pixel_in[..., 1] = input_y
        return pixel_in


def reproject_to_radial(data, wcs, out_shape=None, dpa=None, delongation=None,
        ref_pa=100, ref_elongation=13, ref_x=None, ref_y=None):
    if out_shape is None:
        # Defaults that are decent for a full-res WISPR-I image
        out_shape = list(data.shape)
        out_shape[1] -= 250
        out_shape[1] *= 2
        out_shape[0] += 350
    if dpa is None:
        dpa = wcs.wcs.cdelt[1] * 1.5
    if delongation is None:
        delongation = wcs.wcs.cdelt[0] * .75
    if ref_x is None:
        ref_x = 0
    if ref_y is None:
        ref_y = out_shape[0] // 2
    transformer = RadialTransformer(
            ref_pa=ref_pa, ref_y=ref_y, dpa=-dpa,
            ref_elongation=ref_elongation, ref_x=ref_x, delongation=delongation,
            wcs_in=wcs)
    reprojected = np.zeros(out_shape)
    reproject.adaptive.deforest.map_coordinates(data.astype(float),
            reprojected, transformer, out_of_range_nan=True,
            center_jacobian=False)
    return reprojected, transformer


def reproject_from_radial(data, wcs, out_shape=None, dpa=None, delongation=None,
        ref_pa=100, ref_elongation=13, ref_x=None, ref_y=None):
    if out_shape is None:
        # Defaults that are decent for a full-res WISPR-I image
        out_shape = list(data.shape)
        out_shape[1] -= 250
        out_shape[1] *= 2
        out_shape[0] += 350
    if dpa is None:
        dpa = wcs.wcs.cdelt[1] * 1.5
    if delongation is None:
        delongation = wcs.wcs.cdelt[0] * .75
    if ref_x is None:
        ref_x = 0
    if ref_y is None:
        ref_y = out_shape[0] // 2
    transformer = InverseRadialTransformer(
            ref_pa=ref_pa, ref_y=ref_y, dpa=-dpa,
            ref_elongation=ref_elongation, ref_x=ref_x, delongation=delongation,
            wcs_in=wcs)
    reprojected = np.zeros(out_shape)
    reproject.adaptive.deforest.map_coordinates(data.astype(float),
            reprojected, transformer, out_of_range_nan=True,
            center_jacobian=False)
    return reprojected, transformer


def label_radial_axes(transformer, ax=None):
    if ax is None:
        ax = plt.gca()
    
    xmin, xmax = ax.get_xlim()
    emin, emax = transformer.all_pix2world([xmin, xmax], [1, 1], 0)[0]
    emin = int(np.ceil(emin/10)) * 10
    emax = int(np.floor(emax/10)) * 10
    spacing = 10 if (emax - emin) < 80 else 20
    tick_values = range(emin, emax+1, spacing)
    xtick_locs = [transformer.all_world2pix(elongation, 0)[0]
                  for elongation in tick_values]
    xtick_labels = [f"{elongation}°" for elongation in tick_values]
    ax.set_xticks(xtick_locs, xtick_labels)
    ax.set_xlabel("Elongation")
    
    ymin, ymax = ax.get_ylim()
    pmin, pmax = transformer.all_pix2world([1, 1], [ymin, ymax], 0)[1]
    if pmin > pmax:
        pmin, pmax = pmax, pmin
    pmin = int(np.ceil(pmin/10)) * 10
    pmax = int(np.floor(pmax/10)) * 10
    spacing = 10 if (pmax - pmin) < 80 else 20
    tick_values = range(pmin, pmax+1, spacing)
    ytick_locs = [transformer.all_world2pix(30, pa)[1]
                  for pa in tick_values]
    ytick_labels = [f"{pa}°" for pa in tick_values]
    ax.set_yticks(ytick_locs, ytick_labels)
    ax.set_ylabel("Position Angle")


def produce_radec_for_hp_wcs(wcs_hp, ref_wcs_hp=None, ref_wcs_ra=None,
        ref_hdr=None):
    """Produces an RA/Dec WCS for an HP WCS, from a pair of RA/Dec and HP WCSs
    
    The indended use case is producing composite images, where an output HP WCS
    is constructed from scratch, and a corresponding RA/Dec WCS in the same
    projection is desired. To produce it, the RA/Dec and HP WCSs of one of the
    input images are used.
    """
    if ref_hdr:
        with utils.ignore_fits_warnings():
            ref_wcs_hp = WCS(ref_hdr)
            ref_wcs_ra = WCS(ref_hdr, key='A')
    # Begin to create the output WCS
    wcs_ra = wcs_hp.deepcopy()
    wcs_ra.wcs.ctype = 'RA---ARC', 'DEC--ARC'
    # Update the reference coordinate to the RA/Dec coordinate corresponding to
    # the original HP reference coordinate.
    wcs_ra.wcs.crval = ref_wcs_ra.all_pix2world(
            *ref_wcs_hp.all_world2pix(*wcs_hp.wcs.crval, 0), 0)
    wcs_ra.wcs.cdelt = -wcs_hp.wcs.cdelt[0], wcs_hp.wcs.cdelt[1]
    
    # We now need to find the rotation of the RA/Dec frame. We do that
    # iteratively, using a set of reference HP coordinates for which we compute
    # the corresponding RA/Dec coordinates using the reference WCSs
    pts_x = np.linspace(50, ref_wcs_hp.pixel_shape[0] - 50, 5)
    pts_y = np.linspace(50, ref_wcs_hp.pixel_shape[1] - 50, 5)
    pts_x, pts_y = np.meshgrid(pts_x, pts_y)
    
    pts_hp = ref_wcs_hp.all_pix2world(pts_x, pts_y, 0)
    pts_ra = ref_wcs_ra.all_pix2world(pts_x, pts_y, 0)
    pts_x, pts_y = wcs_hp.all_world2pix(*pts_hp, 0) 
    
    def f(angle):
        angle = angle[0]
        wcs_ra.wcs.pc = np.array([[np.cos(angle), -np.sin(angle)],
                                  [np.sin(angle), np.cos(angle)]])
        pts_ra_trial = wcs_ra.all_pix2world(pts_x, pts_y, 0)
        dra = pts_ra_trial[0] - pts_ra[0]
        ddec = pts_ra_trial[1] - pts_ra[1]
        return np.sum(np.square(dra)) + np.sum(np.square(ddec))
    
    res = scipy.optimize.minimize(f, 0, bounds=[[-np.pi, np.pi]])
    angle = res.x[0]
    wcs_ra.wcs.pc = np.array([[np.cos(angle), -np.sin(angle)],
                              [np.sin(angle), np.cos(angle)]])
    return wcs_ra
