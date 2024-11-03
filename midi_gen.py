import time

import rtmidi

midiout = rtmidi.MidiOut()  # type: ignore
available_ports = midiout.get_ports()

if available_ports:
    print(available_ports)
    midiout.open_port(0)
else:
    midiout.open_virtual_port("My virtual output")

with midiout:
    while True:
        note_on = [0x90, 60, 112]  # channel 1, middle C, velocity 112
        note_off = [0x80, 60, 0]
        print("Sending note on")
        midiout.send_message(note_on)
        time.sleep(0.5)
        print("Sending note off")
        midiout.send_message(note_off)
        time.sleep(0.1)
