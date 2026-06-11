# Fix LWCC path issues before importing anything else
import os
from pathlib import Path
Path(os.path.join(str(Path.home()), ".lwcc/weights")).mkdir(parents=True, exist_ok=True)

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import io
import base64
import numpy as np
import matplotlib.pyplot as plt
from PIL import Image
import uvicorn
from crowd_counter import CrowdCounter
import logging
import traceback
import time

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Crowd Counting API", version="1.0.0")

# Enable CORS for Streamlit frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global model instance
crowd_counter = None
model_loaded = False

@app.on_event("startup")
async def startup_event():
    """Initialize the crowd counting model on startup"""
    global crowd_counter, model_loaded
    try:
        logger.info("Starting model initialization...")
        
        # Try different model configurations if one fails
        model_configs = [
            ("DM-Count", "SHA"),
            ("CSRNet", "SHA"), 
            ("SFANet", "SHA"),
            ("CSRNet", "SHB")
        ]
        
        for model_name, weights in model_configs:
            try:
                logger.info(f"Trying {model_name} with {weights} weights...")
                crowd_counter = CrowdCounter(model_name=model_name, model_weights=weights)
                crowd_counter.load_model()
                
                if crowd_counter.model is not None:
                    model_loaded = True
                    logger.info(f"Model loaded successfully: {model_name} ({weights})")
                    break
                    
            except Exception as e:
                logger.warning(f"Failed to load {model_name} ({weights}): {e}")
                continue
        
        if not model_loaded:
            logger.error("Failed to load any model configuration")
            
    except Exception as e:
        logger.error(f"Startup failed: {e}")
        logger.error(traceback.format_exc())

def save_uploaded_file(upload_file: UploadFile) -> str:
    """Save uploaded file to temporary directory"""
    try:
        # Create uploads directory if it doesn't exist
        os.makedirs("uploads", exist_ok=True)
        
        # Generate unique filename with timestamp
        timestamp = str(int(time.time()))
        file_extension = upload_file.filename.split('.')[-1]
        unique_filename = f"{timestamp}_{upload_file.filename}"
        file_path = f"uploads/{unique_filename}.{file_extension}"
        
        # Save file
        with open(file_path, "wb") as buffer:
            content = upload_file.file.read()
            buffer.write(content)
        
        logger.info(f"File saved: {file_path}")
        return file_path
        
    except Exception as e:
        logger.error(f"Error saving file: {e}")
        raise HTTPException(status_code=500, detail="Failed to save uploaded file")

def density_map_to_base64(density_map: np.ndarray) -> str:
    """Convert density map numpy array to base64 encoded image string"""
    try:
        # Create figure
        plt.figure(figsize=(12, 8))
        plt.imshow(density_map, cmap='hot', interpolation='nearest')
        plt.colorbar(label='Density', shrink=0.8)
        plt.title('Crowd Density Map', fontsize=14)
        plt.axis('off')
        
        # Save to bytes buffer
        buffer = io.BytesIO()
        plt.savefig(buffer, format='png', bbox_inches='tight', dpi=150, 
                   facecolor='white', edgecolor='none')
        buffer.seek(0)
        
        # Convert to base64
        image_base64 = base64.b64encode(buffer.getvalue()).decode()
        plt.close()  # Important: close the figure to free memory
        
        return f"data:image/png;base64,{image_base64}"
        
    except Exception as e:
        logger.error(f"Error creating density map image: {e}")
        return None

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "Crowd Counting API is running!", 
        "status": "active",
        "model_loaded": model_loaded
    }

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    global crowd_counter, model_loaded
    
    if not model_loaded or not crowd_counter or not crowd_counter.model:
        return {
            "status": "unhealthy",
            "model_status": "not loaded",
            "error": "Model initialization failed"
        }
    
    return {
        "status": "healthy",
        "model_status": "loaded",
        "model_name": crowd_counter.model_name,
        "model_weights": crowd_counter.model_weights
    }

@app.post("/predict")
async def predict_crowd_count(file: UploadFile = File(...)):
    """
    Predict crowd count from uploaded image
    
    Args:
        file: Uploaded image file
        
    Returns:
        JSON response with count and density map
    """
    # Check if model is loaded
    if not model_loaded or not crowd_counter or not crowd_counter.model:
        logger.error("Model not loaded - returning 503")
        raise HTTPException(
            status_code=503, 
            detail="Model not loaded. Please check server logs and restart."
        )
    
    # Validate file
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    
    # Check file extension instead of content-type (more reliable)
    allowed_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff'}
    file_ext = os.path.splitext(file.filename.lower())[1]
    
    if file_ext not in allowed_extensions:
        raise HTTPException(
            status_code=400, 
            detail=f"File type {file_ext} not supported. Use: {', '.join(allowed_extensions)}"
        )
    
    file_path = None
    try:
        # Save uploaded file
        file_path = save_uploaded_file(file)
        logger.info(f"Processing image: {file_path}")
        
        # Get prediction with density map using your existing method
        result = crowd_counter.count_people(file_path, return_density_map=True)
        
        if result is None:
            raise HTTPException(status_code=500, detail="Failed to process image - model returned None")
        
        # Handle the result format from your crowd_counter
        if isinstance(result, tuple):
            count, density_map = result
        else:
            count = result
            density_map = None
        
        logger.info(f"Model prediction: {count}")
        
        # Convert density map to base64 image if available
        density_map_image = None
        if density_map is not None:
            density_map_image = density_map_to_base64(density_map)
        
        response = {
            "success": True,
            "estimated_count": round(float(count), 2),
            "density_map": density_map_image,
            "model_info": {
                "model_name": crowd_counter.model_name,
                "model_weights": crowd_counter.model_weights
            },
            "processing_time": "N/A"  # You can add timing if needed
        }
        
        logger.info(f"Prediction successful: {count:.2f}")
        return JSONResponse(content=response)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Prediction error: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Prediction failed: {str(e)}")
    
    finally:
        # Always clean up the uploaded file
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"Cleaned up file: {file_path}")
            except Exception as e:
                logger.warning(f"Failed to clean up file {file_path}: {e}")

@app.post("/predict-batch")
async def predict_batch(files: list[UploadFile] = File(...)):
    """
    Predict crowd count for multiple images
    
    Args:
        files: List of uploaded image files (max 10)
        
    Returns:
        JSON response with counts for all images
    """
    if not model_loaded or not crowd_counter or not crowd_counter.model:
        raise HTTPException(status_code=503, detail="Model not loaded")
    
    if len(files) > 10:  # Limit batch size
        raise HTTPException(status_code=400, detail="Maximum 10 files allowed")
    
    if len(files) == 0:
        raise HTTPException(status_code=400, detail="No files provided")
    
    results = []
    file_paths = []
    
    try:
        # Validate and save all files first
        for file in files:
            if not file.filename:
                raise HTTPException(status_code=400, detail="Invalid file provided")
                
            # Check file extension
            allowed_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff'}
            file_ext = os.path.splitext(file.filename.lower())[1]
            
            if file_ext not in allowed_extensions:
                raise HTTPException(
                    status_code=400, 
                    detail=f"File {file.filename} has unsupported type {file_ext}"
                )
            
            file_path = save_uploaded_file(file)
            file_paths.append(file_path)
        
        # Get predictions for all images
        logger.info(f"Processing batch of {len(file_paths)} images")
        counts = crowd_counter.count_multiple_images(file_paths)
        
        if counts is None:
            raise HTTPException(status_code=500, detail="Batch processing failed")
        
        # Format results
        for i, (file, count) in enumerate(zip(files, counts)):
            results.append({
                "filename": file.filename,
                "estimated_count": round(float(count), 2),
                "status": "success"
            })
        
        logger.info(f"✅ Batch processing successful: {len(results)} images")
        
        return JSONResponse(content={
            "success": True,
            "results": results,
            "total_images": len(files),
            "model_info": {
                "model_name": crowd_counter.model_name,
                "model_weights": crowd_counter.model_weights
            }
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Batch prediction error: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Batch prediction failed: {str(e)}")
    
    finally:
        # Always clean up files
        for file_path in file_paths:
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    logger.info(f"Cleaned up file: {file_path}")
                except Exception as e:
                    logger.warning(f"Failed to clean up file {file_path}: {e}")

@app.get("/models")
async def get_available_models():
    """Get information about available models"""
    return {
        "available_models": [
            {
                "name": "DM-Count",
                "weights": ["SHA", "SHB", "QNRF"],
                "description": "Distribution Matching for Crowd Counting",
                "recommended": True
            },
            {
                "name": "CSRNet", 
                "weights": ["SHA", "SHB"],
                "description": "Congested Scene Recognition Network",
                "recommended": False
            },
            {
                "name": "SFANet",
                "weights": ["SHA", "SHB", "QNRF"], 
                "description": "Scale-aware Feature Aggregation Network",
                "recommended": False
            },
            {
                "name": "Bayesian",
                "weights": ["SHA", "SHB"],
                "description": "Bayesian Crowd Counting",
                "recommended": False
            }
        ],
        "current_model": {
            "name": crowd_counter.model_name if crowd_counter else None,
            "weights": crowd_counter.model_weights if crowd_counter else None
        } if model_loaded else None
    }

if __name__ == "__main__":
    print("Starting Crowd Counting API...")
    print("Make sure LWCC is properly installed: pip install lwcc")
    print("API will be available at: http://localhost:8000")
    print("API docs will be available at: http://localhost:8000/docs")
    
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=8000, 
        log_level="info",
        reload=False  # Set to True for development
    )
