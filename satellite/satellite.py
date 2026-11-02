
import time
import zmq

context = zmq.Context()
sender = context.socket(zmq.PUSH)

sender.connect("tcp://10.10.1.1:5555")

observation = {
    "satellite_id": "SAT-001",
    "observation_id": 1001,
    "latitude": 35.8,
    "longitude": 12.45,
    "wind_speed_ms": 18.5,
    "wind_direction_deg": 240,
    "sequence_number": 1
}

print("[SATELLITE] Connexion au canal...")

time.sleep(1)

sender.send_json(observation)

print("[SATELLITE] Observation envoyée !")
print(observation)

sender.close()
context.term()
