import numpy
import xarray
import rasterio
from numba import njit, prange
import stmetrics
from dtaidistance import dtw
import pandas as pd
from geopandas import GeoDataFrame
from google.colab import drive
drive.mount('/content/drive')
import cc3d
import fastremap
from pyproj import CRS
from sklearn.metrics.pairwise import euclidean_distances
from math import exp

def snitc(dataset, ki, m, distance_calculation, weight_twdtw, nodata=0, scale=10000, iter=10, pattern="hexagonal",
          output="shp", window=None, max_dist=None, max_step=None, 
          max_diff=None, penalty=None, psi=None, pruning=False):
    """This function create spatial-temporal superpixels using a Satellite \
    Image Time Series (SITS). Version 1.4
    :param image: SITS dataset.
    :type image: Rasterio dataset object or a xarray.DataArray.
    :param k: Number or desired superpixels. (Qual o número de superpixels \
    desejados?)
    :type k: int
    :param m: Compactness value. Bigger values led to regular superpixels. \
    não podendo ser 0
    :type m: int
    :param nodata: If you dataset contain nodata, it will be replace by \
    this value. This value is necessary to be possible the use the \
    DTW distance. Ideally your dataset must not contain nodata. (Quantidade \
    de valores NoData (valores nulos))
    :type nodata: float
    :param scale: Adjust the time series, to 0-1. Necessary to distance \
    calculation.
    :type scale: int
    :param iter: Number of iterations to be performed. Default = 10.
    :type iter: int
    :param pattern: Type of pattern initialization. Hexagonal (default) or\
    regular (as SLIC).
    :type pattern: int
    :param output: Type of output to be produced. Default is shp (Shapefile).\
    The two possible values are shp and matrix (returns a numpy array).
    :type output: string
    :param window: Only allow for maximal shifts from the two diagonals \
    smaller than this number. It includes the diagonal, meaning that an \
    Euclidean distance is obtained by setting window=1.
    :param max_dist: Stop if the returned values will be larger than \
    this value.
    :param max_step: Do not allow steps larger than this value.
    :param max_diff: Return infinity if length of two series is larger.
    :param penalty: Penalty to add if compression or expansion is applied.
    :param psi: Psi relaxation parameter (ignore start and end of matching). \
    Useful for cyclical series.
    
    :returns segmentation: Segmentation produced.
    ..Note::
        Reference: Soares, A. R., Körting, T. S., Fonseca, L. M. G., Bendini, \
        H. N. `Simple Nonlinear Iterative Temporal Clustering. \
        <https://ieeexplore.ieee.org/document/9258957>`_ \
        IEEE Transactions on Geoscience and Remote, 2020 (Early Access).
    """
    print('Simple Non-Linear Iterative Temporal Clustering V 1.4')

    fast = False
    try:
        fast = True
    except ImportError:
        logger.debug('DTAIDistance C-OMP library not available')
        fast = False

    if isinstance(dataset, rasterio.io.DatasetReader):
        try:
            # READ FILE
            meta = dataset.profile  # get image metadata
            transform = meta["transform"]
            crs = meta["crs"]
            img = dataset.read().astype(float)
            img[img == dataset.nodata] = numpy.nan

        except:
            Exception('Sorry we could not read your dataset.')
    elif isinstance(dataset, xarray.DataArray):
        try:
            # READ FILE
            transform = dataset.transform
            crs = dataset.crs
            img = dataset.values

        except:
            Exception('Sorry we could not read your dataset.')
    else:
        TypeError("Sorry we can't read this type of file. \
                  Please use Rasterio or xarray")

    # Normalize data
    for band in range(img.shape[0]):
        img[numpy.isnan(img)] = nodata
        img[band, :] = (img[band, :])/scale

    # Get image dimensions
    bands = img.shape[0]
    rows = img.shape[1]
    columns = img.shape[2]

    if pattern == "hexagonal":
        C, S, l, d, k = init_cluster_hex(rows, columns, ki, img, bands)
    elif pattern == "regular":
        C, S, l, d, k = init_cluster_regular(rows, columns, ki, img, bands)
    else:
        print("Unknow patter. We are using hexagonal")
        C, S , l, d, k = init_cluster_hex(rows, columns, ki, img, bands)
    
    # Start clustering
    for n in range(iter):
        for kk in prange(k):
            # Get subimage around cluster
            rmin = int(numpy.floor(max(C[kk, bands]-S, 0)))
            rmax = int(numpy.floor(min(C[kk, bands]+S, rows))+1)
            cmin = int(numpy.floor(max(C[kk, bands+1]-S, 0)))
            cmax = int(numpy.floor(min(C[kk, bands+1]+S, columns))+1)

            # Create subimage 2D numpy.array
            subim = img[:, rmin:rmax, cmin:cmax]

            # get cluster centres
            # Average time series
            c_series = C[kk, :subim.shape[0]]

            # X-coordinate
            ic = int(numpy.floor(C[kk, subim.shape[0]])) - rmin
            # Y-coordinate
            jc = int(numpy.floor(C[kk, subim.shape[0]+1])) - cmin

            # Calculate Spatio-temporal distance
            try:
                D = distance_fast(c_series, ic, jc, subim, S, m, rmin, cmin,
                                  distance_calculation, weight_twdtw,
                                  window=window, max_dist=max_dist,
                                  max_step=max_step, 
                                  max_diff=max_diff,
                                  penalty=penalty,
                                  psi=psi)

            except:
                D = distance(c_series, ic, jc, subim, S, m, rmin, cmin, 
                             window=window, max_dist=max_dist,
                             max_step=max_step, 
                             max_diff=max_diff,
                             penalty=penalty, psi=psi)


            subd = d[rmin:rmax, cmin:cmax]
            subl = l[rmin:rmax, cmin:cmax]

            # Check if Distance from new cluster is smaller than previous
            subl = numpy.where(D < subd, kk, subl)
            subd = numpy.where(D < subd, D, subd)

            # Replace the pixels that had smaller difference
            d[rmin:rmax, cmin:cmax] = subd
            l[rmin:rmax, cmin:cmax] = subl

        # Update Clusters
        C = update_cluster(img, l, rows, columns, bands, k)

        

    # Remove noise from segmentation
    labelled = postprocessing(l, S)

    # Metrics for validation
    metrics = {"STD": [numpy.std(labelled)], "Median": [numpy.median(labelled)], "Mean": [numpy.mean(labelled)]}
    label = {"metrics"}
    df = pd.DataFrame(data=metrics, index=label)
    print(df)

    if output == "shp":
        segmentation = write_pandas(labelled, transform, crs)
        return segmentation
    else:
        # Return labeled numpy.array for visualization on python
        return labelled


@njit(fastmath=True)
def init_cluster_hex(rows, columns, ki, img, bands):
    """This function initialize the clusters for SNITC\
    using a hexagonal pattern.
    :param rows: Number of rows of image.
    :type rows: int
    :param columns: Number of columns of image.
    :type columns: int
    :param ki: Number of desired superpixel.
    :type ki: int
    :param img: Input image.
    :type img: numpy.ndarray
    :param bands: Number of bands (lenght of time series).
    :type bands: int
    :returns C: ND-array containing cluster centres information.
    :returns S: Spacing between clusters.
    :returns l: Matrix label.
    :returns d: Distance matrix from cluster centres.
    :returns k: Number of superpixels that will be produced.
    """
    N = rows * columns

    # Setting up SNITC
    S = (rows*columns / (ki * (3**0.5)/2))**0.5

    # Get nodes per row allowing a half column margin
    nodeColumns = round(columns/S - 0.5)

    # Given an integer number of nodes per row recompute S
    S = columns/(nodeColumns + 0.5)

    # Get number of rows of nodes allowing 0.5 row margin top and bottom
    nodeRows = round(rows/((3)**0.5/2*S))
    vSpacing = rows/nodeRows

    # Recompute k
    k = nodeRows * nodeColumns
    c_shape = (k, bands+3)
    # Allocate memory and initialise clusters, labels and distances
    # Cluster centre data  1:times is mean on each band of series
    # times+1 and times+2 is row, col of centre, times+3 is No of pixels
    C = numpy.zeros(c_shape)
    # Matrix labels.
    labelled = -numpy.ones(img[0, :, :].shape)

    # Pixel distance matrix from cluster centres.
    d = numpy.full(img[0, :, :].shape, numpy.inf)

    # Initialise grid
    kk = 0
    r = vSpacing/2
    for ri in prange(nodeRows):
        x = ri
        if x % 2:
            c = S/2
        else:
            c = S

        for ci in range(nodeColumns):
            cc = int(numpy.floor(c))
            rr = int(numpy.floor(r))
            ts = img[:, rr, cc]
            st = numpy.append(ts, [rr, cc, 0])
            C[kk, :] = st
            c = c+S
            kk = kk+1

        r = r+vSpacing

    st = None
    # Cast S
    S = round(S)

    return C, S, labelled, d, k


@njit(fastmath=True)
def init_cluster_regular(rows, columns, ki, img, bands):
    """This function initialize the clusters for SNITC using a square pattern.
    :param rows: Number of rows of image.
    :type rows: int
    :param columns: Number of columns of image.
    :type columns: int
    :param ki: Number of desired superpixel.
    :type ki: int
    :param img: Input image.
    :type img: numpy.ndarray
    :param bands: Number of bands (lenght of time series).
    :type bands: int
    :returns C: ND-array containing cluster centres information.
    :returns S: Spacing between clusters.
    :returns l: Matrix label.
    :returns d: Distance matrix from cluster centres.
    :returns k: Number of superpixels that will be produced.
    """
    N = rows * columns

    # Setting up SLIC
    S = int((N/ki)**0.5)
    base = int(S/2)

    # Recompute k
    k = int(numpy.floor(rows/base)*numpy.floor(columns/base))
    c_shape = (k, bands+3)

    # Allocate memory and initialise clusters, labels and distances.
    # Cluster centre data 1:times is mean on each band of series
    C = numpy.zeros(c_shape)

    # Matrix labels.
    labelled = -numpy.ones(img[0, :, :].shape)

    # Pixel distance matrix from cluster centres.
    d = numpy.full(img[0, :, :].shape, numpy.inf)

    vSpacing = int(numpy.floor(rows / ki**0.5))
    hSpacing = int(numpy.floor(columns / ki**0.5))

    kk = 0

    # Initialise grid
    for x in range(base, rows, vSpacing):
        for y in range(base, columns, hSpacing):
            cc = int(numpy.floor(y))
            rr = int(numpy.floor(x))
            ts = img[:, int(x), int(y)]
            st = numpy.append(ts, [int(x), int(y), 0])
            C[kk, :] = st
            kk = kk+1

        w = S/2

    st = None

    return C, S, labelled, d, kk


def distance_fast(c_series, ic, jc, subim, S, m, rmin, cmin, 
                  distance_calculation, weight_twdtw,  
                  window=None, max_dist=None, max_step=None, 
                  max_diff=None, penalty=None, psi=None):
    """This function computes the spatial-temporal distance between \
    two pixels using the dtw distance with C implementation.
    :param c_series: average time series of cluster.
    :type c_series: numpy.ndarray
    :param ic: X coordinate of cluster center.
    :type ic: int
    :param jc: Y coordinate of cluster center.
    :type jc: int
    :param subim: Block of image from the cluster under analysis.
    :type subim: int
    :param S: Pattern spacing value.
    :type S: int
    :param m: Compactness value.
    :type m: float
    :param rmin: Minimum row.
    :type rmin: int
    :param cmin: Minimum column.
    :type cmin: int
    :param window: Only allow for maximal shifts from the two diagonals \
    smaller than this number. It includes the diagonal, meaning that an \
    Euclidean distance is obtained by setting window=1.
    :param max_dist: Stop if the returned values will be larger than \
    this value.
    :param max_step: Do not allow steps larger than this value.
    :param max_diff: Return infinity if length of two series is larger.
    :param penalty: Penalty to add if compression or expansion is applied.
    :param psi: Psi relaxation parameter (ignore start and end of matching).
        Useful for cyclical series.
    :returns D:  numpy.ndarray distance.
    """
    from dtaidistance import dtw

    # Normalizing factor
    m = m/10
    
    # Initialize submatrix
    ds = numpy.zeros([subim.shape[1], subim.shape[2]])

    # Tranpose matrix to allow dtw fast computation with dtaidistance
    linear = subim.transpose(1, 2, 0).reshape(subim.shape[1]*subim.shape[2],
                                              subim.shape[0])
    
    merge = numpy.vstack((linear, c_series)).astype(numpy.double)

    # Compute dtw distances (Calculate Temporal Distance)
    c = dtw.distance_matrix_fast(merge, block=((0, merge.shape[0]),
                                 (merge.shape[0] - 1, merge.shape[0])),
                                 compact=True, parallel=True, window=window, 
                                 max_dist=max_dist, max_step=max_step,
                                 max_length_diff=max_diff, penalty=penalty,
                                 psi=psi)
    
    
    c1 = numpy.frombuffer(c)
    
    dc = c1.reshape(subim.shape[1], subim.shape[2])

    x = numpy.arange(subim.shape[1])
    y = numpy.arange(subim.shape[2])
    xx, yy = numpy.meshgrid(x, y, sparse=True, indexing='ij')

    # Calculate Spatial Distance
    ds = (((xx-ic)**2 + (yy-jc)**2)**0.5)

    if distance_calculation == "dtw":
        # Calculate SPatial-temporal distance
        D = (dc)/m+(ds/S)
        
    elif distance_calculation == "twdtw":
        timeseries = linear
        pattern = [c_series]

        psi = euclidean_distances(pattern, timeseries).reshape(subim.shape[1], subim.shape[2])

        if weight_twdtw == "logistic":
            # logistic weight inclination
            alpha = -0.1
            # midpoint of logistic weight
            beta = 100
            # Function for calculating the logistic temporal weight
            logistic_weight = ( 1 / (1 + numpy.exp(alpha*(psi - beta))))
            # Creating an average TW weight value to weight in the DTW distance matrix
            weight_fun = dc + logistic_weight

        else:
            # Function for calculating the linear temporal weight
            linear_weight = psi
            # Creating an average TW weight value to weight in the DTW distance matrix
            weight_fun = dc + linear_weight

        # Calculate SPatial-temporal distance WITH TW
        D = (weight_fun)/m + (ds/S)

    else:
        print("Choose a spatio-temporal distance calculation method (dtw or twdtw)")

    return D


def distance(c_series, ic, jc, subim, S, m, rmin, cmin,
             window=None, max_dist=None, max_step=None, 
             max_diff=None, penalty=None, psi=None, pruning=False):
    """This function computes the spatial-temporal distance between \
    two pixels using the DTW distance.
    :param c_series: average time series of cluster.
    :type c_series: numpy.ndarray
    :param ic: X coordinate of cluster center.
    :type ic: int
    :param jc: Y coordinate of cluster center.
    :type jc: int
    :param subim: Block of image from the cluster under analysis.
    :type subim: int
    :param S: Pattern spacing value.
    :type S: int
    :param m: Compactness value.
    :type m: float
    :param rmin: Minimum row.
    :type rmin: int
    :param cmin: Minimum column.
    :type cmin: int
    :param window: Only allow for maximal shifts from the two diagonals \
    smaller than this number. It includes the diagonal, meaning that an \
    Euclidean distance is obtained by setting window=1.
    :param max_dist: Stop if the returned values will be larger than \
    this value.
    :param max_step: Do not allow steps larger than this value.
    :param max_diff: Return infinity if length of two series is larger.
    :param penalty: Penalty to add if compression or expansion is applied.
    :param psi: Psi relaxation parameter (ignore start and end of matching).
        Useful for cyclical series.
    :param use_pruning: Prune values based on Euclidean distance.
    :returns D: numpy.ndarray distance.
    """
    from dtaidistance import dtw

    # Normalizing factor
    m = m/10

    # Initialize submatrix
    ds = numpy.zeros([subim.shape[1], subim.shape[2]])
    
    # Tranpose matrix to allow dtw fast computation with dtaidistance
    linear = subim.transpose(1, 2, 0).reshape(subim.shape[1]*subim.shape[2],
                                              subim.shape[0])
    merge = numpy.vstack((linear, c_series)).astype(numpy.double)
    
    c = dtw.distance_matrix(merge, block=((0, merge.shape[0]),
                        (merge.shape[0] - 1, merge.shape[0])),
                        compact=True, use_c=True, parallel=True, use_mp=True)
    c1 = numpy.array(c)
    dc = c1.reshape(subim.shape[1], subim.shape[2])

    x = numpy.arange(subim.shape[1])
    y = numpy.arange(subim.shape[2])
    xx, yy = numpy.meshgrid(x, y, sparse=True, indexing='ij')
    # Calculate Spatial Distance
    ds = (((xx-ic)**2 + (yy-jc)**2)**0.5)
    # Calculate SPatial-temporal distance
    D = (dc)/m+(ds/S)

    return D


@njit(parallel=True, fastmath=True)
def update_cluster(img, la, rows, columns, bands, k):
    """This function update clusters.
    :param img: Input image.
    :type img: numpy.ndarray
    :param la: Matrix label.
    :type la: numpy.ndarray
    :param rows: Number of rows of image.
    :type rows: int
    :param columns: Number of columns of image.
    :type columns: int
    :param bands: Number of bands (lenght of time series).
    :type bands: int
    :param k: Number of superpixel.
    :type k: int
    :returns C_new: ND-array containing updated cluster centres information.
    """
    c_shape = (k, bands+3)

    # Allocate array info for centres
    C_new = numpy.zeros(c_shape)

    # Update cluster centres with mean values
    for r in prange(rows):
        for c in range(columns):
            tmp = numpy.append(img[:, r, c], numpy.array([r, c, 1]))
            kk = int(la[r, c])
            C_new[kk, :] = C_new[kk, :] + tmp

    # Compute mean
    for kk in prange(k):
        C_new[kk, :] = C_new[kk, :]/C_new[kk, bands+2]

    tmp = None

    return C_new


def postprocessing(raster, S):
    """Post processing function to enforce connectivity.
    :param raster: Labelled image.
    :type raster: numpy.ndarray
    :param S: Spacing between superpixels.
    :type S: int
    :returns final: Labelled image with connectivity enforced.
    """
    import fastremap
    from rasterio import features

    for i in range(10):

        raster, remapping = fastremap.renumber(raster, in_place=True)

        # Remove spourious regions generated during segmentation
        cc = cc3d.connected_components(raster.astype(dtype=numpy.uint16),
                                       connectivity=6)

        T = int((S**2)/2)

        # Use Connectivity as 4 to avoid undesired connections
        raster = features.sieve(cc.astype(dtype=rasterio.int32), T,
                                out=numpy.zeros(cc.shape,
                                                dtype=rasterio.int32),
                                connectivity=4)

    return raster


def write_pandas(segmentation, transform, crs):
    """This function creates a GeoPandas DataFrame \
    of the segmentation.
    :param segmentation: Segmentation numpy array.
    :type segmentation: numpy.ndarray
    :param transform: Transformation parameters.
    :type transform: list
    :param crs: Coordinate Reference System.
    :type crs: PROJ4 dict
    :returns gdf: Segmentation as a geopandas geodataframe.
    """
    import geopandas
    import rasterio.features
    from shapely.geometry import shape

    mypoly = []

    # Loop to oconvert raster conneted components to
    # polygons using rasterio features
    seg = segmentation.astype(dtype=numpy.float32)
    for vec in rasterio.features.shapes(seg, transform=transform):
        mypoly.append(shape(vec[0]))

    gdf = geopandas.GeoDataFrame(geometry=mypoly, crs=crs)
    gdf.crs = crs

    mypoly = None

    return gdf

### Change only from here down


# PATH OF IMAGE STACK IN TIF FORMAT
dataset = xarray.open_rasterio("/content/drive/MyDrive/IC-2021-2022/Stack_NDVI_tif/stack_NDVI_separate_2019_20.tif")

# Input parameters for SNITC
ki = 20
m = 5
nodata = float(0)
scale = 1000
iter = 10
pattern = "regular"
output = "shp"
distance_calculation = "twdtw"
weight_twdtw = "logistic"

running_snitc = snitc(dataset, ki, m, distance_calculation, weight_twdtw, nodata, scale, iter, pattern, output, window=None, max_dist=None, max_step=None, max_diff=None, penalty=None, psi=None, pruning=False)
print(running_snitc)

# Export SNITC output as SHP
outfp = "/content/drive/MyDrive/IC-2021-2022/teste_teste_teste_2.shp"
running_snitc.to_file(outfp)