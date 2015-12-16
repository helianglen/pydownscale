__author__ = 'tj'
import numpy
import matplotlib
#matplotlib.use('Agg')
from matplotlib import pyplot
from data import DownscaleData, read_nc_files
from downscale import DownscaleModel
import sys
from scipy.linalg import solve_sylvester
import time

def hascomplex(x):
    if numpy.iscomplex(x).sum() > 0:
        return True
    else:
        return False

def center_data(X, y):
    X_mean = X.mean(axis=0)
    #X -= X_mean
    X_frob = numpy.diag(1./numpy.sqrt(numpy.sum(X**2, axis=0)))
    X = X.dot(X_frob)
    y_mean = y.mean(axis=0)
    y -= y_mean
    return X, y, X_mean, y_mean, X_frob

class pMSSL:
    def __init__(self, max_epochs=1000, quite=True, lambd=1e-3, 
        gamma=1e-3, wadmm=True, omega_epochs=100, w_epochs=100, 
        rho=1.):
        self.lambd = float(lambd)
        self.max_epochs = max_epochs
        self.quite = quite
        self.gamma = gamma
        self.wadmm = wadmm
        self.omega_epochs = omega_epochs
        self.w_epochs = w_epochs
        self.rho = rho

    def fit(self, X, y, epsomega=1e-3 ,epsw=1e-3):
        X, y, self.X_mean, self.y_mean, self.X_frob = center_data(X, y)
        Xy = X.T.dot(y)
        self.K = y.shape[1]
        self.n = y.shape[0]
        self.d = X.shape[1]
        if not hasattr(self, 'Omega'):
            self.Omega = numpy.eye(self.K)
        if not hasattr(self, 'W'):
            self.W = numpy.zeros(shape=(self.d, self.K))

        prev_omega = self.Omega.copy()
        prev_w = self.W.copy()
        costdiff = 10
        omegatime = 0.
        wtime = 0.
        start_time = time.time()
        costs = []
        print "Number of tasks: %i, Number of dimensions: %i, Number of observations: %i, Lambda: %0.4f, Gamma: %0.4f" % (self.K, self.d, self.n, self.lambd, self.gamma)

        for t in range(self.max_epochs):
            prev_omega = self.Omega.copy()
            prev_w = self.W.copy()
            tw = time.time()
            if self.wadmm:
                self.W = self._w_update_admm(X, y, rho=self.rho)
            else:
                self.W = self._w_update(X, y)
            wtime += (time.time() - tw)
            to = time.time()
            self.Omega = self._omega_update(X, y, self.Omega, rho=self.rho)
            omegatime += (time.time() - to)
            omega_diff = numpy.linalg.norm(self.Omega - prev_omega, 2)
            w_diff = numpy.linalg.norm(prev_w - self.W, 2)
            
            if (omega_diff < epsomega) and (w_diff < epsw):
                break

            if not self.quite:
                print "iteration %i, w zeros: %i, omega zeros: %i" % (t, numpy.sum(self.W == 0), numpy.sum(self.Omega == 0))
                print "Omega diff:", omega_diff, "\tW Diff:", w_diff

            # 24 Hours is almost up
            if (time.time() - start_time) > (20. * 60 * 60):
                break

        minutes = (time.time() - start_time) / 60.
        print "Gamma: %0.4f, Lambda: %0.4f, Converged in %i iterations, %2.3f Minutes" % (self.gamma, self.lambd, t, minutes)
        #print "Amount of time to train Omega: %f" % omegatime
        #print "Amount of time to train W:     %f" % wtime

    def cost(self, X, y, W, Omega):
        cost, _ = self._w_cost(X, y, W)
        if numpy.sum(Omega) != 0:
            cost -= self.K/2.*numpy.log(numpy.linalg.det(Omega))
        cost += self.lambd * numpy.linalg.norm(Omega, 1)
        return cost 

    def shrinkage_threshold(self, a, alpha):
        return numpy.maximum(numpy.zeros(shape=a.shape), a-alpha) - numpy.maximum(numpy.zeros(shape=a.shape), -a-alpha)

    def _softthres(self, x, thres):
        if x > thres:
            return x - thres
        elif numpy.abs(x) < thres:
            return 0
        else:
            return x+thres

    def softthreshold(self, X, thres):
        return numpy.piecewise(X, 
            [X > thres, numpy.abs(X) <= thres, X < -thres], 
            [lambda X: X - thres, 0, lambda X: X+thres])

    def _omega_update(self, X, y, Omega, rho, epochs=100):
        #Z = numpy.zeros(shape=(self.K, self.K))
        Z = self.Omega.copy()  ## warm start
        U = numpy.zeros(shape=(self.K, self.K))
        j = 0
        resid = []
        # Sp = self.W.T.dot(self.W)   # why is this in the MSSL paper?
        S = numpy.cov(self.W.T)

        rho = self.rho
        epsabs = 1e-2
        epsrel = 1e-4

        for j in range(self.omega_epochs):
            try:
                L, Q = numpy.linalg.eigh(rho * (Z - U) - S)
            except numpy.linalg.LinAlgError:
                #raise numpy.linalg.LinAlgError("Gamma: %f Lambda: %f W:\n" % (self.gamma, self.lambd) + numpy.array_str(self.W) + "\n")
                pass

            Omega_tilde = numpy.eye(self.K)

            numpy.fill_diagonal(Omega_tilde, (L + numpy.sqrt(L**2 + 4*rho))/(2*rho))
            Omega = Q.dot(Omega_tilde).dot(Q.T)
            Z_prev = Z.copy()
            Z = self.softthreshold(Omega + U, self.lambd/rho)
            U = U + Omega - Z

            dualresid = numpy.linalg.norm(-rho * (Z - Z_prev), 'fro')
            primalresid = numpy.linalg.norm(Omega - Z, 'fro')

            epspri = self.n * epsabs + epsrel * numpy.max([numpy.linalg.norm(Omega, 'fro'), numpy.linalg.norm(Z, 'fro'), 0])
            epsdual = self.n * epsabs + epsrel * numpy.linalg.norm(rho*U, 'fro')

            #if (j % 50) == 1 and (not self.quite):
            #print "omega update:", j, "Dualresid:", dualresid, "Dual EPS:", epsdual

            comp = numpy.iscomplex(Z).sum()
            #print "ZPrev", Z_prev[:5, :5]
            #print "Z", Z[:5,:5]
            #print "S", S[:5, :5]
            if (dualresid < epsdual) and (primalresid < epspri):
                break
        #print "Omega converged in ", (j+1)
        return Z


   # Lets save the proximal descent update
    def _w_update(self, X, y):
        costdiff = 1
        W = self.W
        j = 0
        t0 = time.time()
        tk = 1/(numpy.linalg.norm(X.T.dot(X), 1)) # tk exits in (0, 1/||X.T*X||)
        XX = X.T.dot(X)
        XY = X.T.dot(y)
        while costdiff > 1e-5:
            cost, gmat = self._w_cost(X, y, W, XX=XX, XY=XY)
            W = self.shrinkage_threshold(W - tk*gmat, alpha=self.gamma*tk)
            if j == 0:
                costdiff = numpy.abs(cost)
            else:
                costdiff = numpy.abs(costprev - cost)

            costprev = cost
            if (j > self.w_epochs):
                #print "Warning: W did not converge."
                break
            j += 1

        return W

    # Lets parallelize this
    def _w_cost(self, X, y, W,  XX=None, XY=None):
        XW = X.dot(W)
        if XX is None:
            XX = X.T.dot(X)
        if XY is None:
            XY = X.T.dot(y)
        f = 0.5*numpy.linalg.norm(y - XW, 2)
        #f = (y-XW).T.dot((y-XW)) / (2*len(y))
        f += self.lambd*numpy.trace(W.dot(self.Omega).dot(W.T))
        gmat = (XX.dot(W) - XY)/len(y)  # the gradients
        gmat += 2*W.dot(self.Omega) # *self.lambd
        return numpy.sum(f), gmat

    def _w_update_admm(self, X, y, rho):
        #Z = numpy.zeros(shape=(self.d, self.K))
        Z = self.W.copy()  ## warm start
        U = numpy.zeros(shape=(self.d, self.K))
        j = 0
        XX = X.T.dot(X)  # dxd
        Xy = X.T.dot(y)  # dxk
        Theta = self.W.copy()      # dxk
        epsabs = 1e-2
        epsrel = 1e-4
        tsum = 0.
        for j in range(self.w_epochs): 
            comp = numpy.iscomplex(Theta).sum()
            prevTheta = Theta.copy()
            C = Xy + rho * (Z - U)
            t0 = time.time()
            Theta = solve_sylvester(XX + rho * numpy.eye(XX.shape[0]), 2*self.Omega, C)
            #print "Time to solve sylvester", time.time() - t0
            tsum += time.time() - t0
            Z_prev = Z.copy()
            Z = self.softthreshold(Theta + U, self.gamma/rho)
            U = U + Theta - Z
            if (j % 100 == 0) and (not self.quite):
                print j, numpy.linalg.norm(prevTheta - Theta, 2)

            dualresid = numpy.linalg.norm(-rho*(Z - Z_prev), 2)
            primalresid = numpy.linalg.norm(Theta - Z, 2)
            epspri = self.n * epsabs + epsrel * numpy.max([numpy.linalg.norm(Theta, 2), numpy.linalg.norm(Z, 2), 0])
            epsdual = self.n * epsabs + epsrel * numpy.linalg.norm(rho*U,2)

            if (dualresid < epsdual) and (primalresid < epspri):
                #print "Converged in %i" % j
                break

            if numpy.abs(Z).mean() > 1e10:
                #print j, rho
                pass
            #if j % 5 == 0:
            if not self.quite:
                print "Iteration: %i, Rho: %2.2f, Dualresid: %2.4f, EPS_Dual: %2.4f, PriResid: %2.4f, EPS_Pri: %2.4f" % (j, rho, dualresid, epsdual, primalresid, epspri)

        return Z

    def predict(self, X):
        X = X.dot(self.X_frob)
        return X.dot(self.W) + self.y_mean


def mse(y1, y2):
    return numpy.mean((y1 - y2)**2)

def test2():
    numpy.random.seed(1)
    n = 200
    d = 20
    k = 15
    l = 1e2
    g = 0.0383814370407
    train = 70
    tt = time.time()
    W = numpy.random.normal(size=(d, k))

    rows1 = numpy.random.choice(range(d), 5)
    rows2 = numpy.random.choice(range(d), 6)

    W[rows1, :4] += numpy.random.normal(0, 2, size=(len(rows1), 1))
    W[rows2, 5:10] += numpy.random.normal(0, 2, size=(len(rows2), 1))

    
    X = numpy.random.uniform(-1, 1, size=(n, d))
    #X_prev = X.copy()
    #X -= X.mean(axis=0)
    X = X.dot(numpy.diag(1/numpy.sqrt(sum(X**2))))
    y = X.dot(W) + numpy.random.normal(0, 0.1, size=(n, k))

    Z = []
    #lspace = numpy.linspace(0,10,3)
    lspace = [0.1]
    for l in lspace:
        mssl = pMSSL(max_epochs=50, quite=True, gamma=g, lambd=l)
        mssl.fit(X[:train], y[:train], rho=1e-2,  wadmm=True)
        yhat = mssl.predict(X[train:])
        Z.append(numpy.sum(mssl.Omega == 0))
        print Z[-1]

    #pyplot.plot(lspace, Z)
    #pyplot.show() 

    from scipy.stats import spearmanr, pearsonr
    from sklearn.linear_model import Lasso, LassoCV
    print "MSSL: Spearman", spearmanr(yhat[:,0], y[train:,0])[0]
    print "MSSL: MSE", mse(yhat[:,0], y[train:,0])

    m = LassoCV(normalize=True)
    m.fit(X[:train, :], y[:train, 0])
    yhat = m.predict(X[train:, :])
    print m.coef_.T[:10]
    print "Lasso: Spearman ", pearsonr(yhat, y[train:, 0])[0]
    print "Lasso: MSE", mse(yhat, y[train:, 0])

    pyplot.subplot(1,2,1)
    pyplot.imshow(numpy.linalg.inv(mssl.Omega), interpolation="none", cmap="Reds")
    pyplot.subplot(1,2,2)
    pyplot.imshow(mssl.W, interpolation="none", cmap="Reds")

    pyplot.title("Lamdba %f, Gamma %f" % (l, g))
    pyplot.show()


def climatetest():
    import time
    t0 = time.time()
    cmip5_dir = "/scratch/vandal.t/cmip5/access1-3/"
    cpc_dir = "/scratch/vandal.t/merged/"

    # climate model data, monthly
    cmip5 = read_nc_files(cmip5_dir)
    cmip5.load()
    cmip5 = cmip5.resample('MS', 'time', how='mean')   ## try to not resample

    # daily data to monthly
    cpc = read_nc_files(cpc_dir)
    cpc.load()
    monthlycpc = cpc.resample('MS', dim='time', how='mean')  ## try to not resample

    print "Data Loaded: %d seconds" % (time.time() - t0)
    data = DownscaleData(cmip5, monthlycpc)
    #data.normalize_monthly()

    # print "Data Normalized: %d" % (time.time() - t0)
    X = data.get_X()
    lats = data.observations.lat
    lons = data.observations.lon
    j = len(lats)/2
    i = len(lons)/2
    Y = data.observations.loc[{'lat': lats[j:j+5], 'lon': lons[i:i+5]}].to_array().values.squeeze()
    Y = Y.reshape(Y.shape[0], Y.shape[1]*Y.shape[2])
    
    mssl = pMSSL()
    mssl.fit(X[:70, :5000], Y[:70])
    yhat = mssl.predict(X[70:, :5000])
    #pyplot.plot(yhat[70:,0])
    #pyplot.plot(Y[70:, 0], color='red')
    #pyplot.show()

if __name__ == "__main__":
    ms = test2()
    #climatetest()

