import zmq


# ==========================================
# CONFIGURATION
# ==========================================

SOURCE_ADDRESS = "tcp://10.10.1.1:5555"
DESTINATION_ADDRESS = "tcp://10.10.2.10:5556"

EXPECTED_BLOCKS = 5

# Capturer le premier bloc IQ
CAPTURE_BLOCK = 1

# Remplacer le troisième bloc par le premier
REPLAY_AT_BLOCK = 3


# ==========================================
# REPLAY ATTACK SIMULATOR
# ==========================================

def main():

    context = zmq.Context()

    receiver = context.socket(zmq.PULL)
    sender = context.socket(zmq.PUSH)

    receiver.setsockopt(zmq.LINGER, 0)

    sender.setsockopt(zmq.IMMEDIATE, 1)
    sender.setsockopt(zmq.SNDTIMEO, 5000)
    sender.setsockopt(zmq.LINGER, 1000)

    receiver.bind(SOURCE_ADDRESS)
    sender.connect(DESTINATION_ADDRESS)

    print("[CANAL] Mode REPLAY ATTACK")
    print(f"[CANAL] Source : {SOURCE_ADDRESS}")
    print(f"[CANAL] Destination : {DESTINATION_ADDRESS}")
    print("[CANAL] Capture bloc #1 ; replay au bloc #3")
    print()

    received = 0
    forwarded = 0
    replayed = 0

    captured_iq = None

    try:

        while received < EXPECTED_BLOCKS:

            # Recevoir les échantillons IQ
            incoming_iq = receiver.recv()

            received += 1

            print(
                f"[RX] Bloc #{received} | "
                f"{len(incoming_iq)} octets"
            )

            # Enregistrer la première trame IQ
            if received == CAPTURE_BLOCK:

                captured_iq = bytes(incoming_iq)

                print(
                    "[CAPTURE] Premier bloc IQ enregistré"
                )

            # Attaque : substituer le bloc 3
            if received == REPLAY_AT_BLOCK:

                if captured_iq is None:
                    raise RuntimeError(
                        "Aucune trame capturée"
                    )

                output_iq = captured_iq

                replayed += 1

                print(
                    f"[REPLAY] Bloc #{received} remplacé "
                    f"par une copie exacte du bloc "
                    f"#{CAPTURE_BLOCK}"
                )

                print(
                    "[REPLAY] Aucun bit modifié ; "
                    "CRC32 et Ed25519 conservés"
                )

                status = "REPLAY"

            else:

                output_iq = incoming_iq
                status = "INTACT"

            # Envoyer vers Station
            try:

                sender.send(output_iq)

            except zmq.Again:

                print(
                    "[ERROR] Timeout ZeroMQ vers Station"
                )
                break

            forwarded += 1

            print(
                f"[TX] Bloc #{received} | "
                f"status={status} | "
                f"{len(output_iq)} octets"
            )

            print()

    except KeyboardInterrupt:

        print("\n[CANAL] Arrêt manuel")

    finally:

        receiver.close()
        sender.close()
        context.term()

        print("========== BILAN REPLAY ==========")
        print(f"Blocs reçus : {received}")
        print(f"Blocs remis à ZeroMQ : {forwarded}")
        print(f"Blocs rejoués : {replayed}")
        print("==================================")


if __name__ == "__main__":
    main()
