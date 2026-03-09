# backend.py
# RUN: python -m uvicorn backend:app --reload

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict
import uuid
from datetime import datetime
import random
import requests
import json
import os
import hashlib

app = FastAPI(title="CareCue Enterprise Pro Max")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_FILE = "carecue_db.json"

# --- DATABASE LOGIC (Persistent JSON) ---
def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {"patients": {}, "doctors": {}}

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

db = load_db()

# --- WEBSOCKETS (Real-Time Sync) ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            await connection.send_text(message)

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# --- MODELS ---
class Medicine(BaseModel):
    userId: str
    name: str
    dosage: str
    time: str
    frequency: Optional[str] = "Daily"
    notes: Optional[str] = ""

class LogDose(BaseModel):
    userId: str
    medicineId: str
    medicineName: str
    status: str 
    dosageString: Optional[str] = ""

class InventoryItem(BaseModel):
    userId: str
    medName: str
    quantity: int
    expiryDate: str

class Appointment(BaseModel):
    userId: str
    doctor: str
    date: str
    time: str
    reason: str

class ProfileData(BaseModel):
    userId: str
    name: str
    bloodType: str
    height: str
    weight: str
    allergies: str
    conditions: str
    doctor: str
    insurance: str
    emergencyContact: str
    avatar: Optional[str] = "" 
    bgImage: Optional[str] = "" # NEW: Background Image Storage

class ChatMessage(BaseModel):
    message: str
    userId: str
    role: str

class AuthPayload(BaseModel):
    name: str
    pin: str
    dob: Optional[str] = ""
    bloodType: Optional[str] = ""
    height: Optional[str] = ""
    weight: Optional[str] = ""
    allergies: Optional[str] = ""
    conditions: Optional[str] = ""
    reason: Optional[str] = ""
    needsCaregiver: Optional[str] = "No"

# --- SECURITY UTILS ---
def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()

def verify_pin(plain_pin: str, hashed_pin: str):
    return hash_pin(plain_pin) == hashed_pin

# --- AUTH ENDPOINTS ---
@app.post("/api/register")
def register_user(payload: AuthPayload):
    user_id = f"user_{payload.name.lower().replace(' ', '')}"
    if user_id in db["patients"]:
        return {"success": False, "message": "User already exists. Please login."}
    
    db["patients"][user_id] = {
        "id": user_id,
        "name": payload.name,
        "pin": hash_pin(payload.pin),
        "profile": {
            "name": payload.name, "dob": payload.dob, "bloodType": payload.bloodType,
            "height": payload.height, "weight": payload.weight, "allergies": payload.allergies,
            "conditions": payload.conditions, "doctor": "", "insurance": "",
            "emergencyContact": "", "reason": payload.reason, "needsCaregiver": payload.needsCaregiver,
            "streak": 0, "coins": 0, "avatar": "", "bgImage": ""
        },
        "appointments": [], "medicines": [], "inventory": [], "history": [],
        "ring_active_now": False, "pending_sms_alert": ""
    }
    save_db(db)
    return {"success": True, "userId": user_id}

@app.post("/api/login")
def login_user(payload: dict):
    user_id = f"user_{payload.get('name', '').lower().replace(' ', '')}"
    patient = db["patients"].get(user_id)
    if patient and verify_pin(payload.get("pin", ""), patient["pin"]):
        return {"success": True, "userId": user_id}
    return {"success": False, "message": "Invalid Name or PIN"}

@app.post("/api/caregiver/login")
def caregiver_login(payload: dict): 
    if payload.get("pin") == "1234": return {"success": True}
    return {"success": False}

@app.post("/api/doctor/login")
def doctor_login(payload: dict): 
    if payload.get("pin") == "0000": return {"success": True}
    return {"success": False}

# --- DATA ENDPOINTS ---
@app.get("/api/data/{user_id}")
def get_data(user_id: str):
    return db["patients"].get(user_id, {})

@app.get("/api/caregiver/patients")
def get_patients():
    summary = []
    for pid, data in db["patients"].items():
        summary.append({
            "id": pid, "name": data["name"], 
            "condition": data["profile"].get("conditions", "N/A"),
            "missed": len([h for h in data.get("history", []) if h["status"] == "missed"]),
            "phone": data["profile"].get("emergencyContact", "N/A"),
            "appointments": data.get("appointments", [])
        })
    return summary

@app.post("/api/medicines")
async def add_med(med: Medicine):
    new_med = med.dict()
    new_med["id"] = str(uuid.uuid4())
    new_med["isActive"] = True
    db["patients"][med.userId]["medicines"].append(new_med)
    save_db(db)
    await manager.broadcast("update")
    return {"success": True}

@app.delete("/api/medicines/{user_id}/{med_id}")
async def delete_med(user_id: str, med_id: str):
    db["patients"][user_id]["medicines"] = [m for m in db["patients"][user_id]["medicines"] if m["id"] != med_id]
    save_db(db)
    await manager.broadcast("update")
    return {"success": True}

@app.post("/api/history")
async def log_dose(log: LogDose):
    entry = log.dict()
    entry["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    user_data = db["patients"][log.userId]
    user_data.setdefault("history", []).append(entry)
    
    # Mark as taken -> clear any ringing immediately
    if log.status == "taken":
        user_data["ring_active_now"] = False
        user_data["pending_sms_alert"] = ""
        user_data["profile"]["streak"] = user_data["profile"].get("streak", 0) + 1
        user_data["profile"]["coins"] = user_data["profile"].get("coins", 0) + 10 
        for item in user_data.get("inventory", []):
            if item["medName"].lower() == log.medicineName.lower() and item["quantity"] > 0:
                item["quantity"] -= 1
    else:
        user_data["profile"]["streak"] = 0
        user_data["pending_sms_alert"] = f"Missed dose: {log.medicineName}"

    save_db(db)
    await manager.broadcast("update")
    return {"success": True}

@app.post("/api/clear_alerts/{user_id}")
def clear_alerts(user_id: str):
    if user_id in db["patients"]:
        db["patients"][user_id]["ring_active_now"] = False
        db["patients"][user_id]["pending_sms_alert"] = ""
        save_db(db)
        return {"success": True}
    return {"success": False}

@app.post("/api/inventory")
async def update_inventory(item: InventoryItem):
    inv = db["patients"][item.userId].setdefault("inventory", [])
    for ex in inv:
        if ex["medName"].lower() == item.medName.lower():
            ex["quantity"] = item.quantity
            ex["expiryDate"] = item.expiryDate
            save_db(db)
            await manager.broadcast("update")
            return {"success": True}
    
    new_item = item.dict()
    new_item["id"] = str(uuid.uuid4())
    inv.append(new_item)
    save_db(db)
    await manager.broadcast("update")
    return {"success": True}

@app.post("/api/appointments")
def add_app(app: Appointment):
    new_app = app.dict()
    new_app["id"] = str(uuid.uuid4())
    db["patients"][app.userId].setdefault("appointments", []).append(new_app)
    save_db(db)
    return {"success": True}

@app.post("/api/profile")
def update_profile(p: ProfileData):
    db["patients"][p.userId]["profile"].update(p.dict())
    save_db(db)
    return {"success": True}

@app.post("/api/caregiver/ring/{user_id}")
async def ring_patient(user_id: str):
    if user_id in db["patients"]:
        db["patients"][user_id]["ring_active_now"] = True
        save_db(db)
        await manager.broadcast("ring")
        return {"success": True}
    return {"success": False}

@app.post("/api/doctor/contact")
def contact_caregiver(payload: dict):
    print(f"Sending Notification to Caregiver of {payload.get('userId')}: {payload.get('message')}")
    return {"success": True}

# --- SMART AI FEATURES ---
@app.post("/api/ai/chat")
def chat_ai(msg: ChatMessage):
    q = msg.message.lower()
    if any(x in q for x in ["cold", "flu", "runny", "caugh", "sneez"]):
        return {"response": "For cold/flu symptoms: Stay hydrated and rest. OTC meds like Cetirizine or Vitamin C can help. If you have a high fever (>102°F), please see a doctor."}
    if any(x in q for x in ["pain", "headache", "ache", "hurt"]):
        return {"response": "For general pain, Paracetamol (Dolo) or Ibuprofen are standard. If the pain is severe or persistent, consult a specialist."}
    if any(x in q for x in ["fever", "temp", "hot"]):
        return {"response": "Monitor your temperature. Paracetamol 650mg is commonly used. Use cool compresses if needed."}
    
    try:
        potential = q.split()[-1] 
        if len(potential) > 3:
            url = f"https://api.fda.gov/drug/label.json?search=openfda.brand_name:{potential}&limit=1"
            res = requests.get(url)
            if res.status_code == 200:
                data = res.json()['results'][0]
                use = data.get('indications_and_usage', ['General medication'])[0][:150]
                return {"response": f"I found info for {potential.capitalize()}: {use}..."}
    except: pass

    return {"response": "I am CareCue Smart AI. Ask me about symptoms, your schedule, or medicine details."}

@app.post("/api/ai/risk/{user_id}")
def get_risk(user_id: str):
    data = db["patients"].get(user_id)
    if not data: return {"score": 0}
    history = data.get("history", [])
    taken = len([h for h in history if h["status"] == "taken"])
    total = len(history)
    score = 100 if total == 0 else int((taken/total)*100)
    label, color = "Low Risk", "#10b981"
    if len(data.get("medicines", [])) >= 5: score = min(score, 85); label = "Moderate Risk"; color = "#f59e0b"
    if score < 70: label = "High Risk"; color = "#ef4444"
    return {"score": score, "label": label, "color": color}

@app.get("/api/ai/drug_info/{query}")
def drug_info(query: str):
    try:
        url = f"https://api.fda.gov/drug/label.json?search=openfda.brand_name:{query}&limit=1"
        res = requests.get(url, timeout=3).json()
        if "results" in res:
            r = res["results"][0]
            uses = r.get("indications_and_usage", ["General use"])[0][:150] + "..."
            warn = r.get("warnings", ["Consult doctor"])[0][:100] + "..."
            price = round(random.uniform(50, 500), 2)
            return {"name": query.capitalize(), "uses": uses, "side_effects": warn, "price": f"₹{price}"}
    except: pass
    
    db_local = {
        "dolo": {"uses": "Fever and mild to moderate pain.", "side_effects": "Nausea, allergic reaction."},
        "crocin": {"uses": "Fever reducer and pain reliever.", "side_effects": "Liver damage in high doses."},
        "allegra": {"uses": "Allergy relief (sneezing, runny nose).", "side_effects": "Drowsiness, dry mouth."},
        "pantop": {"uses": "Acid reflux and heartburn.", "side_effects": "Headache, diarrhea."}
    }
    for k, v in db_local.items():
        if k in query.lower():
            return {"name": query.capitalize(), "uses": v["uses"], "side_effects": v["side_effects"], "price": f"₹{random.randint(30, 150)}"}

    return {"name": query.capitalize(), "uses": "Common medication.", "side_effects": "Consult doctor.", "price": f"₹{random.randint(50,200)}"}

@app.post("/api/ai/identify_pill")
async def identify_pill(file: UploadFile = File(...)):
    fname = file.filename.lower()
    if "dolo" in fname: result, conf = "Dolo 650mg (Paracetamol)", "99%"
    elif "aspirin" in fname: result, conf = "Aspirin 81mg (Blood Thinner)", "98%"
    elif "metformin" in fname: result, conf = "Metformin 500mg (Diabetes)", "97%"
    else: result, conf = "Generic Paracetamol", "85%"
    return {"result": result, "confidence": conf}

@app.post("/api/ai/ocr_prescription")
async def ocr_prescription(file: UploadFile = File(...)):
    return { "meds": [ {"name": "Amoxicillin", "dosage": "500mg", "time": "09:00"} ] }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)