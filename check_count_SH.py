from scipy.io import loadmat

mat_data = loadmat(r"ShanghaiTech\part_A\train_data\ground-truth\GT_IMG_26.mat")
image_info = mat_data["image_info"]

annotated_data = (image_info[0][0][0][0])
print(len(annotated_data[0]))