import pickle
from matplotlib import pyplot as plt
import sys
import numpy as np

def load_checkpoint(filename):
    with open(filename, "rb") as file:
        chkpt = pickle.load(file)
        return chkpt

chk=load_checkpoint(sys.argv[1])

rho = chk["primitive"][:,:,0]
xvel = chk["primitive"][:,:,1]
pressure = chk["primitive"][:,:,2]

#xposs=np.linspace(0.0,1.0,chk["config"]["domain"]["num_zones"][0])

#print(chk["config"])

print(rho.shape)
plt.imshow(rho.T,vmin=0.0,vmax=1.2)
#plt.plot(xposs,xvel)
#plt.plot(xposs,pressure)

plt.colorbar()

#plt.title(f"{chk['time']}")
plt.savefig(f"chkpt.{sys.argv[1].split('.')[1]}.png",dpi=600)
#plt.show()