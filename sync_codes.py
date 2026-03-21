import json
import random
import string
import os

def gen_code(length=4):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def generate_sets():
    base_dir = '/Users/sadiq_nasser/Desktop/axiom_v4/server'
    
    # 1. BETA Codes (App Activation)
    beta_file = os.path.join(base_dir, 'app_licenses.json')
    beta_set = []
    for i in range(1, 101):
        code = f"BETA-{gen_code(4)}-{gen_code(4)}-{gen_code(4)}"
        beta_set.append({
            "code": code,
            "device_id": None,
            "activated_at": None,
            "is_used": False,
            "label": f"beta_user_{i}"
        })
    
    with open(beta_file, 'w', encoding='utf-8') as f:
        json.dump({"licenses": beta_set}, f, indent=2, ensure_ascii=False)
        
    # 2. AXVIP Codes (Features)
    vip_file = os.path.join(base_dir, 'activation_codes.json')
    vip_set = []
    for i in range(1, 101):
        code = f"AXVIP-{gen_code(4)}-{gen_code(4)}-{gen_code(4)}"
        vip_set.append({
            "code": code,
            "is_active": True,
            "feature_flags": ["all"],
            "max_devices": 1,
            "used_by": [],
            "label": f"vip_user_{i}"
        })
        
    with open(vip_file, 'w', encoding='utf-8') as f:
        json.dump({"codes": vip_set}, f, indent=2, ensure_ascii=False)

    # 3. Save a readable list for the user
    with open('/Users/sadiq_nasser/Desktop/axiom_v4/generated_codes_list.txt', 'w', encoding='utf-8') as f:
        f.write("=== 100 BETA CODES (App Activation) ===\n")
        for item in beta_set:
            f.write(f"{item['code']}\n")
        f.write("\n=== 100 AXVIP CODES (Feature Upgrade) ===\n")
        for item in vip_set:
            f.write(f"{item['code']}\n")

if __name__ == "__main__":
    generate_sets()
    print("Generated 100 BETA and 100 AXVIP codes successfully.")
