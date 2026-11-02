
import zmq

context = zmq.Context()

receiver = context.socket(zmq.PULL)
receiver.bind("tcp://10.10.1.1:5555")

sender = context.socket(zmq.PUSH)
sender.setsockopt(zmq.IMMEDIATE, 1)
sender.setsockopt(zmq.SNDTIMEO, 5000)
sender.connect("tcp://10.10.2.10:5556")

print("[CANAL] Relais IQ BPSK actif")
print("[CANAL] En attente du satellite...")

try:
    while True:
        iq_data = receiver.recv()

        sender.send(iq_data)

        print(
            f"[CANAL] Bloc IQ relayé : "
            f"{len(iq_data)} octets"
        )

except KeyboardInterrupt:
    print("\n[CANAL] Arrêt demandé")

except zmq.Again:
    print("[ERREUR] Station non disponible")

finally:
    receiver.close(linger=0)
    sender.close(linger=2000)
    context.term()
