import os
from pathlib import Path
import lwcc.util.functions

# Fix buggy weights_check in LWCC library that uses root directory "/.lwcc"
def corrected_weights_check(model_name, model_weights):
    home = str(Path.home())
    weights_dir = os.path.join(home, ".lwcc", "weights")
    Path(weights_dir).mkdir(parents=True, exist_ok=True)
    
    file_name = "{}_{}.pth".format(model_name, model_weights)
    url = lwcc.util.functions.build_url(file_name)
    output = os.path.join(weights_dir, file_name)
    
    if not os.path.isfile(output):
        print(file_name, " will be downloaded to ", output)
        import gdown
        gdown.download(url, output, quiet=False)
        
    return output

lwcc.util.functions.weights_check = corrected_weights_check

from lwcc import LWCC
import matplotlib.pyplot as plt
import numpy as np

class CrowdCounter:
    # Class constuctor
    def __init__(self, model_name="CSRNet", model_weights="SHA"):
        """
        Initialize the crowd counter with specified model

        Available models: CSRNet, Bay, DM-Count, SFANet
        Available weights: SHA, SHB, QNRF
        """
        self.model_name = model_name
        self.model_weights = model_weights
        self.model = None
# ----------------------------------------------------------------------------------------------------------------------------------------------------
    # Loading the model using model name and weights
    def load_model(self):
        """Load the pre-trained model"""
        try:
            self.model = LWCC.load_model(
                model_name=self.model_name,
                model_weights=self.model_weights
            )
            print(f"Model {self.model_name} with {self.model_weights} weights loaded successfully!")
        except Exception as e:
            print(f"Error loading model: {e}")
# ----------------------------------------------------------------------------------------------------------------------------------------------------
    # Method to count people from a single image
    def count_people(self, image_path, return_density_map=False):
        """
        Count people in a single image
        By default returning density map is set to false

        Args:
            image_path: Path to the image file
            return_density_map: Whether to return density map along with count

        Returns:
            count (float) or tuple (count, density_map) if density map is set to True
        """
        try:
            if return_density_map:
                count, density_map = LWCC.get_count(
                    image_path,
                    model=self.model,
                    return_density=True
                )
                return count, density_map
            else:
                count = LWCC.get_count(image_path, model=self.model)
                return count
        except Exception as e:
            print(f"Error counting people: {e}")
            return None
# ----------------------------------------------------------------------------------------------------------------------------------------------------
    # Method to count people from multiple images
    def count_multiple_images(self, image_paths, return_density_map=False):
        """
        Count people in multiple images

        Args:
            image_paths: List of image paths
            return_density_map: Whether to return density maps

        Returns:
            List of counts or list of (count, density_map) tuples
        """
        results = []
        try:
            for image_path in image_paths:
                if return_density_map:
                    count, density_map = LWCC.get_count(
                        image_path,
                        model=self.model,
                        return_density=True
                    )
                    results.append((count, density_map))
                else:
                    count = LWCC.get_count(image_path, model=self.model)
                    results.append(count)
            return results
        except Exception as e:
            print(f"Error counting people in multiple images: {e}")
            return None
