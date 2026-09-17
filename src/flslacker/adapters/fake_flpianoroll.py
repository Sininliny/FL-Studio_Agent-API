"""A stdlib-only stand-in for FL's ``flpianoroll`` module.

Used to run the real generated Piano Roll scripts in tests, in ``serve --fake`` and
under FL's embedded interpreter. It models the documented API only; host behaviour
learned in M0 is configurable (see ``FakeHost``). Mocks cannot certify compatibility.
"""

import sys
import types

_NOTE_DEFAULTS = (
    ("number", 60),
    ("time", 0),
    ("length", 96),
    ("group", 0),
    ("pan", 0.5),
    ("velocity", 0.78125),
    ("release", 0.5),
    ("color", 0),
    ("fcut", 0.5),
    ("fres", 0.5),
    ("pitchofs", 0),
    ("slide", False),
    ("porta", False),
    ("muted", False),
    ("selected", False),
    ("repeats", 0),
)
_NOTE_FIELD_NAMES = tuple(name for name, _ in _NOTE_DEFAULTS)
_FLOAT_FIELDS = ("pan", "velocity", "release", "fcut", "fres")


class FakeHost:
    """Host behaviour switches, shared by every object of one fake module."""

    def __init__(self, ppq=96, expose_selected_only=False, float_quantum=None, fail_after_writes=None):
        self.ppq = ppq
        self.expose_selected_only = expose_selected_only
        self.float_quantum = float_quantum
        self.fail_after_writes = fail_after_writes
        self.writes = 0
        self.messages = []
        self.log = []
        self.dialog_responder = None
        self.dialogs = []

    def count_write(self):
        self.writes += 1
        if self.fail_after_writes is not None and self.writes > self.fail_after_writes:
            raise RuntimeError("simulated host failure")


def make_module(host=None, notes=None, markers=None, timeline_selection=(0, 0)):
    """Build a fresh ``flpianoroll`` module object backed by ``host``."""
    host = host or FakeHost()
    module = types.ModuleType("flpianoroll")

    class Note(object):
        def __init__(self, **values):
            object.__setattr__(self, "_values", dict(_NOTE_DEFAULTS))
            object.__setattr__(self, "_live", False)
            for key, value in values.items():
                setattr(self, key, value)

        def __getattr__(self, name):
            values = object.__getattribute__(self, "_values")
            if name in values:
                return values[name]
            raise AttributeError(name)

        def __setattr__(self, name, value):
            if name not in _NOTE_FIELD_NAMES:
                raise AttributeError(name)
            if object.__getattribute__(self, "_live"):
                host.count_write()
            if name in _FLOAT_FIELDS:
                value = float(value)
                if host.float_quantum:
                    value = round(value / host.float_quantum) * host.float_quantum
            elif name in ("slide", "porta", "muted", "selected"):
                value = bool(value)
            else:
                value = int(value)
            object.__getattribute__(self, "_values")[name] = value

        def clone(self):
            copy = Note()
            object.__getattribute__(copy, "_values").update(object.__getattribute__(self, "_values"))
            return copy

        def __dir__(self):
            return list(_NOTE_FIELD_NAMES) + ["clone"]

    class Marker(object):
        def __init__(self, time=0, name="", mode=0, tsnum=4, tsden=4):
            self.time = time
            self.name = name
            self.mode = mode
            self.tsnum = tsnum
            self.tsden = tsden

    class Score(object):
        def __init__(self):
            self._notes = []
            self._markers = []
            self._selection = tuple(timeline_selection)
            self.tsnum = 4
            self.tsden = 4

        @property
        def PPQ(self):
            return host.ppq

        def _exposed(self):
            if host.expose_selected_only and any(n.selected for n in self._notes):
                return [n for n in self._notes if n.selected]
            return self._notes

        @property
        def noteCount(self):
            return len(self._exposed())

        def getNote(self, index):
            return self._exposed()[index]

        def addNote(self, note):
            host.count_write()
            copy = note.clone()
            object.__setattr__(copy, "_live", True)
            self._notes.append(copy)

        def deleteNote(self, index):
            host.count_write()
            target = self._exposed()[index]
            for position, note in enumerate(self._notes):
                if note is target:
                    del self._notes[position]
                    return
            raise IndexError(index)

        @property
        def markerCount(self):
            return len(self._markers)

        def getMarker(self, index):
            return self._markers[index]

        def addMarker(self, marker):
            self._markers.append(marker)

        def deleteMarker(self, index):
            del self._markers[index]

        def clear(self, all=False):
            self.clearNotes(all)
            self.clearMarkers(all)

        def clearNotes(self, all=False):
            if all:
                self._notes = []
            else:
                self._notes = [n for n in self._notes if not n.selected]

        def clearMarkers(self, all=False):
            self._markers = [] if all else self._markers

        def getTimelineSelection(self):
            return self._selection

        def setTimelineSelection(self, start, end):
            self._selection = (start, end)

        def getDefaultNoteProperties(self):
            return Note()

        def getNextFreeGroupIndex(self):
            return max([n.group for n in self._notes] + [0]) + 1

        # test helpers (not part of the FL API)
        def load(self, records):
            self._notes = []
            for record in records:
                note = Note(**record)
                object.__setattr__(note, "_live", True)
                self._notes.append(note)

        def dump(self):
            return [dict(object.__getattribute__(n, "_values")) for n in self._notes]

    class ScriptDialog(object):
        def __init__(self, title, description):
            self.title = title
            self.description = description
            self.inputs = []
            self.values = {}
            self.restoreFormValues = True

        def _add(self, kind, name, value, **extra):
            self.inputs.append(dict(kind=kind, name=name, value=value, **extra))
            self.values[name] = value

        def AddInput(self, name, value, hint=""):
            self._add("input", name, value)

        def AddInputKnob(self, name, value, minimum, maximum, hint=""):
            self._add("knob", name, value, min=minimum, max=maximum)

        def AddInputKnobInt(self, name, value, minimum, maximum, hint=""):
            self._add("knob_int", name, value, min=minimum, max=maximum)

        def AddInputCombo(self, name, options, value, hint=""):
            if isinstance(options, str):
                options = options.split(",")
            self._add("combo", name, value, options=list(options))

        def AddInputCheckbox(self, name, value, hint=""):
            self._add("checkbox", name, value)

        def AddInputText(self, name, value, hint=""):
            self._add("text", name, value)

        def GetInputValue(self, name):
            return self.values[name]

        def execute(self):
            host.dialogs.append(self)
            if host.dialog_responder is None:
                return False
            answer = host.dialog_responder(self)
            if answer is None or answer is False:
                return False
            if isinstance(answer, dict):
                self.values.update(answer)
            return True

    class Utils(object):
        @staticmethod
        def ShowMessage(message):
            host.messages.append(str(message))

        @staticmethod
        def log(message):
            host.log.append(str(message))

        @staticmethod
        def ProgressMsg(message, position, total):
            pass

    score = Score()
    score.load(notes or [])
    for marker in markers or []:
        score.addMarker(Marker(**marker))

    module.Note = Note
    module.Marker = Marker
    module.Score = Score
    module.ScriptDialog = ScriptDialog
    module.Utils = Utils
    module.score = score
    module.host = host
    return module


def run_script(source, module, filename="<slacker script>"):
    """Execute a generated Piano Roll script against ``module`` as FL would."""
    previous = sys.modules.get("flpianoroll")
    sys.modules["flpianoroll"] = module
    try:
        namespace = {"__name__": "__slacker_script__", "__file__": filename}
        exec(compile(source, filename, "exec"), namespace)
        return namespace
    finally:
        if previous is None:
            sys.modules.pop("flpianoroll", None)
        else:
            sys.modules["flpianoroll"] = previous


def pick_option(dialog, name, predicate):
    """Return the index of the first combo option in ``dialog`` matching ``predicate``."""
    for item in dialog.inputs:
        if item["name"] == name:
            for index, option in enumerate(item["options"]):
                if predicate(option):
                    return index
    raise LookupError("no matching option for " + name)
