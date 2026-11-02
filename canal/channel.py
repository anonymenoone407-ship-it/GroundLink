
import zmq

context = zmq.Context()

receiver = context.socket(zmq.PULL)
receiver.bind("tcp://10.10.1.1:5555")

sender = context.socket(zmq.PUSH)
sender.connect("tcp://10.10.2.10:5556")

print("[CANAL] Canal actif...", flush=True)
print("[CANAL] En attente du satellite...", flush=True)

observation = receiver.recv_json()

print("[CANAL] Observation reçue du satellite")
print("[CANAL] Transmission vers la station...")

sender.send_json(observation)

print("[CANAL] Observation transmise !")

receiver.close()
sender.close()
context.term()
