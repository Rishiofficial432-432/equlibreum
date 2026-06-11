import os
import csv
import numpy as np
import scipy.io
from crowd_counter import CrowdCounter  # Assumes you have a CrowdCounter class with a predict method
from glob import glob

def get_actual_count(mat_path):
    mat = scipy.io.loadmat(mat_path)
    # Assuming ground truth is in 'image_info' and points in 'image_info[0][0][0][0][0]'
    # This may need to be adjusted based on your .mat file structure
    points = mat['image_info'][0][0][0][0][0]
    return len(points)

def get_image_and_gt_pairs(images_dir, gt_dir):
    pairs = []
    for img_path in glob(os.path.join(images_dir, '*.jpg')):
        img_name = os.path.basename(img_path)
        gt_name = 'GT_' + img_name.replace('processed_', '').replace('.jpg', '.mat')
        gt_path = os.path.join(gt_dir, gt_name)
        if os.path.exists(gt_path):
            pairs.append((img_path, gt_path, img_name))
    return pairs

def evaluate_dataset(images_dir, gt_dir, model):
    results = []
    pairs = get_image_and_gt_pairs(images_dir, gt_dir)
    for img_path, gt_path, img_name in pairs:
        actual_count = get_actual_count(gt_path)
        predicted_count = model.count_people(img_path)  # Use count_people method
        accuracy = 100 * (1 - abs(predicted_count - actual_count) / actual_count) if actual_count else 0
        results.append({
            'image': img_name,
            'predicted': predicted_count,
            'actual': actual_count,
            'accuracy': accuracy
        })
    return results

def main():
    base_dir = 'D:/ShanghaiTech'
    sets = [
        ('part_A/test_data/images', 'part_A/test_data/ground-truth'),
        ('part_A/train_data/images', 'part_A/train_data/ground-truth'),
        ('part_B/test_data/images', 'part_B/test_data/ground-truth'),
        ('part_B/train_data/images', 'part_B/train_data/ground-truth'),
    ]
    model = CrowdCounter()
    model.load_model()  # Load the model before predictions

    all_results = []
    for img_rel, gt_rel in sets:
        images_dir = os.path.join(base_dir, img_rel)
        gt_dir = os.path.join(base_dir, gt_rel)
        results = evaluate_dataset(images_dir, gt_dir, model)
        all_results.extend(results)

    # Write results to CSV
    with open('count_estimation_results.csv', 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['image', 'predicted', 'actual', 'accuracy'])
        writer.writeheader()
        for row in all_results:
            writer.writerow(row)

    # Compute metrics
    predicted = np.array([r['predicted'] for r in all_results])
    actual = np.array([r['actual'] for r in all_results])
    mse = np.mean((predicted - actual) ** 2)
    mae = np.mean(np.abs(predicted - actual))
    avg_accuracy = np.mean([r['accuracy'] for r in all_results])
    print(f'Total images: {len(all_results)}')
    print(f'MSE: {mse:.2f}')
    print(f'MAE: {mae:.2f}')
    print(f'Average Accuracy: {avg_accuracy:.2f}%')

if __name__ == '__main__':
    main()