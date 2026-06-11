import sys
print("Python version:", sys.version)

try:
    from lwcc import LWCC
    print("✅ LWCC import successful")
    
    # Try to load a simple model
    print("Trying to load CSRNet with SHA weights...")
    model = LWCC.load_model(model_name="CSRNet", model_weights="SHA")
    print(f"✅ Model loaded: {type(model)}")
    
except Exception as e:
    print(f"❌ LWCC error: {e}")
    import traceback
    traceback.print_exc()
