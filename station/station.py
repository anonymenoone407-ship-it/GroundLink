
import zmq

context = zmq.Context()
receiver = context.socket(zmq.PULL)

receiver.bind("tcp://10.10.2.10:5556")

print("[STATION] En attente des observations...", flush=True)

observation = receiver.recv_json()

print("\n[STATION] Observation reçue :")
print(observation)

print("\n[STATION] Réception réussie !")

receiver.close()
context.term()
