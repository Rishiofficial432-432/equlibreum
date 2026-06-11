from scipy.io import loadmat
mat_data = loadmat(r'UCFCrowdCountingDataset_CVPR13\UCF_CC_50\12_ann.mat')
print(type(mat_data))
annPoints = mat_data["annPoints"]
print(annPoints.shape[0])

