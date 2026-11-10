"""The menu's inline form: keys move, edit and change answers in any order; the create form builds the
same command as the questions did. No terminal: keys go to Form.handle, the screen is Form.lines()."""
import pytest

from pvelab import form as formmod, wizard
from pvelab.form import Field, Form

UP, DOWN, LEFT, RIGHT, ENTER, ESC, BS = b"\x1b[A", b"\x1b[B", b"\x1b[D", b"\x1b[C", b"\r", b"\x1b", b"\x7f"


def typed(text):
    return [c.encode() for c in text]


def press(f, *keys):
    for k in keys:
        for b in ([k] if isinstance(k, bytes) else typed(k)):
            r = f.handle(b)
            if r:
                return r
    return None


def simple():
    values = {"n": "20", "os": "a", "ok": False}
    fields = lambda v: [Field("n", "Number", check=lambda a: None if a.isdigit() else "a number"),
                        Field("os", "OS", "choice", [("a", "A"), ("b", "B")]),
                        Field("ok", "Box", "check", text="tick")]
    return Form("T", fields, lambda v, fs: [(f"n={v['n']}", None)], values)


def test_typing_replaces_the_value_then_edits_it():
    f = simple()
    press(f, "15")
    assert f.values["n"] == "15"
    press(f, BS, "6")
    assert f.values["n"] == "16"


def test_up_down_choice_and_box_in_any_order():
    f = simple()
    press(f, DOWN, RIGHT, DOWN, b" ", UP, UP, "3")
    assert f.values == {"n": "3", "os": "b", "ok": True}


def test_enter_stays_on_a_bad_answer_and_run_needs_all_fixed():
    f = simple()
    press(f, "x", ENTER)
    assert f.focus == "n" and any("a number" in line for line in f.lines())
    assert press(f, DOWN, DOWN, DOWN, ENTER) is None and f.focus == "n"   # Run: back to the bad answer
    assert any("fix the answers in yellow" in line for line in f.lines())
    assert press(f, BS, "4", ENTER, ENTER, ENTER, ENTER) == "run"


def test_esc_and_cancel_button_cancel():
    assert press(simple(), ESC) == "cancel"
    assert press(simple(), DOWN, DOWN, DOWN, RIGHT, ENTER) == "cancel"


def test_lines_show_the_preview_and_the_focus():
    f = simple()
    lines = f.lines()
    assert lines[1].startswith("> Number") and "n=20" in "".join(lines)
    f.done = True
    assert not any(line.startswith(">") for line in f.lines()) and "[ Run ]" not in "".join(f.lines())


# --- create


@pytest.fixture
def create(monkeypatch):
    monkeypatch.setattr(wizard, "is_new_net", lambda n: n != "30")
    monkeypatch.setattr(wizard.nets, "cidr_free", lambda n, net: None)
    monkeypatch.setattr(wizard, "os_options", lambda inv=None: [("debian13", "debian-13"), ("ubuntu26", "ubuntu-26"),
                                                       ("dev", "dev  (saved template)")])

    def make():
        c = wizard.CreateForm({"count": {"net30": 15}, "nets": ["net30"], "vms": []})    # by name, as lab_data
        return c, Form("Create", c.fields, c.preview, c.values)
    return make


def labels(f):
    return [x.label for x in f.fields]


def go(f, label):
    """Focus the field with this label (from the top)."""
    while f.focus != f.fields[0].key:
        f.handle(UP)
    while f.by_key.get(f.focus) is None or f.by_key[f.focus].label != label:
        f.handle(DOWN)


def test_create_defaults(create):
    c, f = create()
    assert labels(f) == ["Students", "VMs per student", "Operating system", "Network", "Group work",
                         "ssh from outside", "CPU cores", "Memory", "Disk GB", "Scripts"]
    assert c.argv(c.values) == ["create", "20", "debian13", "--net", "auto", "--expose"]


def test_create_builder_vm(create):
    c, f = create()
    press(f, "1", DOWN, DOWN, RIGHT, RIGHT, DOWN, "2")
    assert "Address range" in labels(f)
    go(f, "ssh from outside")
    press(f, b" ")
    go(f, "CPU cores")
    press(f, "4", DOWN, "4G", DOWN, DOWN, "docker+/x/dev-tools.sh")
    assert c.argv(c.values) == ["create", "1", "dev", "--net", "2", "--cores", "4", "--memory", "4G",
                                "--script", "docker+/x/dev-tools.sh"]


def test_create_existing_net_drops_range_and_group_work(create):
    c, f = create()
    go(f, "Network")
    press(f, "2")
    assert "Address range" in labels(f) and "Group work" in labels(f)
    press(f, BS, "30")
    assert "Address range" not in labels(f) and "Group work" not in labels(f)
    assert "adds to net30 (15 VMs)" in "".join(f.lines())


def test_create_range_typed_for_a_new_name(create):
    c, f = create()
    go(f, "Network")
    press(f, "docker-lab", DOWN, "192.168.50.0/24")
    assert c.argv(c.values)[3:7] == ["--net", "docker-lab", "--cidr", "192.168.50.0/24"]


def test_create_several_vms_each_different(create):
    c, f = create()
    press(f, "2", DOWN, "3")
    assert "Per VM" in labels(f)
    go(f, "Per VM")
    press(f, b" ")
    assert "Operating system" not in labels(f) and "VM -3 OS" in labels(f) and "Scripts -2" in labels(f)
    go(f, "VM -1 OS")
    press(f, RIGHT)
    go(f, "Memory -1")
    press(f, "8G")
    argv = c.argv(c.values)
    assert argv[1:3] == ["2x3", "ubuntu26,debian13,debian13"]
    assert argv[argv.index("--memory") + 1] == "8G,2048,2048"


def test_create_warns_about_expose_without_harden(create):
    c, f = create()
    assert any("without the harden script" in t for t, _ in c.preview(c.values, f.errors()))
    go(f, "Scripts")
    press(f, "harden")
    assert not any("without the harden script" in t for t, _ in c.preview(c.values, f.errors()))


def test_create_bad_answer_hides_the_command(create):
    c, f = create()
    press(f, "0")
    assert "fix the answers in yellow" in c.preview(c.values, f.errors())[0][0]


def test_wizard_uses_the_questions_without_a_terminal(monkeypatch):
    monkeypatch.setattr(formmod.sys.stdin, "isatty", lambda: False, raising=False)
    assert not formmod.fits_screen(10)


def test_create_empty_range_disk_and_scripts_are_fine_but_not_an_empty_count(create):
    c, f = create()
    go(f, "Network")
    press(f, "2")
    assert "Address range" in labels(f) and not f.errors()
    assert "empty: 10.10.2.0/24" in "".join(f.lines())
    go(f, "Students")
    press(f, BS, BS)
    assert f.errors() == {"count": "please answer"}
    assert "fix the answers in yellow" in c.preview(c.values, f.errors())[0][0]


def test_create_empty_text_only_while_empty(create):
    c, f = create()
    go(f, "Scripts")
    assert "empty: none" in f.lines()[[x.label for x in f.fields].index("Scripts") + 1]
    press(f, "harden")
    assert "empty: none" not in "".join(f.lines())


def test_a_long_command_is_wrapped_not_cut(create):
    c, f = create()
    go(f, "Scripts")
    press(f, "docker+/home/seanyeh77/pvelab-scripts/dev-tools.sh")
    lines = f.lines(60)
    below = lines[next(i for i, line in enumerate(lines) if line.startswith("  ---")) + 1:]
    cmd = [line for line in below if line.startswith(("  pvelab create", "      "))]
    assert "dev-tools.sh" in "".join(cmd) and all(len(line) <= 60 for line in cmd)


def test_run_in_the_form_is_not_asked_again(monkeypatch):
    ran = []
    monkeypatch.setattr(wizard, "run_pvelab", lambda argv: ran.append(argv) or 0)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("asked again"))
    wizard.execute(["create", "1", "debian13"], confirmed=True)
    assert ran == [["create", "1", "debian13"]]


def test_create_scripts_for_each_vm_with_a_comma(create):
    c, f = create()
    press(f, "2", DOWN, "2")
    go(f, "Scripts")
    press(f, "harden+docker,harden")
    assert "scripts" not in f.errors() and c.argv(c.values)[-2:] == ["--script", "harden+docker,harden"]
    press(f, BS, BS, BS, BS, BS, BS, "nope")
    assert "unknown script 'nope'" in f.errors()["scripts"]


def test_create_vms_per_student_checked_again_when_students_change(create):
    c, f = create()
    press(f, "20", DOWN, "3")
    assert not f.errors()
    go(f, "Students")
    press(f, "40")
    assert f.errors() == {"per": "40 students x 3 VMs is over 99 VMs: at most 33 students"}


def test_a_tall_form_keeps_the_field_in_view():
    from pvelab.form import window
    lines = [str(i) for i in range(40)]
    assert window(lines, 2, 20)[0] == "0" and "2" in window(lines, 2, 20)
    assert "35" in window(lines, 35, 20) and len(window(lines, 35, 20)) == 20
    assert window(lines[:10], 9, 20) == lines[:10]


def test_ctrl_c_while_drawing_goes_back(monkeypatch):
    from pvelab import ui
    monkeypatch.setattr(ui, "termios", type("T", (), {"tcgetattr": lambda fd: None, "tcsetattr": lambda *a: None,
                                                      "TCSADRAIN": 1}))
    monkeypatch.setattr(ui, "tty", type("T", (), {"setcbreak": lambda fd: None}))
    monkeypatch.setattr(ui.sys.stdin, "fileno", lambda: 0, raising=False)

    def slow():
        raise KeyboardInterrupt
    assert ui.edit_loop(slow, lambda k: "cancel" if k == b"\x03" else None) == ("cancel", 0)


def test_create_scripts_must_be_one_or_one_per_vm(create):
    c, f = create()
    press(f, "2", DOWN, "3")
    go(f, "Scripts")
    press(f, "harden,docker")
    assert "one for each of a student's 3 VMs" in f.errors()["scripts"]


def test_create_network_being_typed_with_regex_characters(create, monkeypatch):
    c, f = create()
    monkeypatch.setattr(c, "multi", True)
    monkeypatch.setattr(wizard, "node_room", lambda: {"a": {"mem": 9000, "disk": 90}})

    def bridge_host(n):
        assert n != "(", "looked up a net that is still being typed"
    monkeypatch.setattr(wizard, "bridge_host", bridge_host)
    go(f, "Network")
    press(f, "(")                                  # not a net yet: Where's pin is not looked up
    assert "net" in f.errors() and "Where" in labels(f)


def test_create_vms_per_student_must_match_an_existing_net(create, monkeypatch):
    monkeypatch.setattr(wizard, "per_student_on", lambda n, rows=None: 1 if n == "30" else 0)    # as create reads it
    c, f = create()
    press(f, "2", DOWN, "2")
    go(f, "Network")
    press(f, "30")
    assert f.errors() == {"per": "net30 has 1 VM per student: answer 1"}


def test_create_per_vm_scripts_take_their_part(create):
    c, f = create()
    press(f, "2", DOWN, "2")
    go(f, "Scripts")
    press(f, "harden+docker,harden")
    go(f, "Per VM")
    press(f, b" ")
    assert c.values["scripts1"] == "harden+docker" and c.values["scripts2"] == "harden" and not f.errors()


def test_create_copy_notes_once_per_template_set(create, monkeypatch):
    c, f = create()
    calls = []
    monkeypatch.setattr(c, "multi", True)
    monkeypatch.setattr(wizard, "bridge_host", lambda n: None)
    monkeypatch.setattr(wizard, "node_room", lambda: {"a": {"mem": 9000, "disk": 90}, "b": {"mem": 9000, "disk": 90}})
    monkeypatch.setattr(wizard, "copy_needs", lambda t, hosts, inv: calls.append(hosts) or [("b", "x is copied to b first")])
    f.refresh()
    go(f, "Where")
    texts = []
    for _ in range(3):
        texts.append([t for t, _ in c.preview(c.values, f.errors())])
        press(f, RIGHT)
    assert calls == [["a", "b"]]                   # auto, all on a, all on b: lvs once
    assert "x is copied to b first" in texts[0] and "x is copied to b first" not in texts[1]


def test_a_cancelled_form_draws_no_preview(monkeypatch):
    from pvelab import form as formmod
    f, drawn = simple(), []
    monkeypatch.setattr(formmod, "edit_loop", lambda render, handle: ("cancel", 0))
    monkeypatch.setattr(formmod, "redraw", lambda out, lines, n: drawn.extend(lines))
    assert f.run() is False and not any("n=" in line for line in drawn)


def test_create_per_vm_answers_are_their_own_after_the_first_tick(create):
    c, f = create()
    press(f, "2", DOWN, "2")
    go(f, "Scripts")
    press(f, "a,b")
    go(f, "Per VM")
    press(f, b" ")                                      # tick: a / b from the common answer
    assert (c.values["scripts1"], c.values["scripts2"]) == ("a", "b")
    go(f, "Scripts -2")
    press(f, "e")
    go(f, "Per VM")
    press(f, b" ")                                      # untick: the common answer, unchanged
    assert c.argv(c.values)[-2:] == ["--script", "a,b"]
    go(f, "Per VM")
    press(f, b" ")                                      # tick again: the VMs' answers as they were left
    assert c.argv(c.values)[-2:] == ["--script", "a,e"]


def test_create_a_vm_added_later_starts_from_the_one_before(create):
    c, f = create()
    press(f, "2", DOWN, "2")
    go(f, "Scripts")
    press(f, "harden,docker")
    go(f, "Per VM")
    press(f, b" ")
    go(f, "Scripts -2")
    press(f, "harden")
    go(f, "VMs per student")
    press(f, "3")
    assert (c.values["scripts2"], c.values["scripts3"]) == ("harden", "harden") and not f.errors()


def test_ctrl_c_on_the_last_redraw_after_run_runs_nothing(monkeypatch):
    from pvelab import form as formmod
    f = simple()
    monkeypatch.setattr(formmod, "edit_loop", lambda render, handle: ("run", 0))

    def redraw(out, lines, n):
        raise KeyboardInterrupt
    monkeypatch.setattr(formmod, "redraw", redraw)
    assert f.run() is False


def test_create_no_range_while_the_network_answer_is_not_a_net(create):
    c, f = create()
    go(f, "Network")
    press(f, "Bad")
    assert "Address range" not in labels(f) and "Group work" not in labels(f) and "net" in f.errors()


def test_create_typing_vms_per_student_keeps_each_vms_answers(create):
    c, f = create()
    press(f, "10", DOWN, "3")
    go(f, "Per VM")
    press(f, b" ")
    go(f, "VM -2 OS")
    press(f, RIGHT)
    go(f, "CPU cores -3")
    press(f, "4")
    go(f, "VMs per student")
    press(f, BS, "2")                                   # empty for a moment, then 2
    assert (c.values["os2"], c.values["cores3"]) == ("ubuntu26", "4") and not f.errors()
    assert c.argv(c.values)[1:3] == ["10x2", "debian13,ubuntu26"]


def test_create_unticking_per_vm_leaves_valid_common_answers(create):
    c, f = create()
    press(f, "2", DOWN, "2")
    go(f, "Per VM")
    press(f, b" ")
    go(f, "VM -2 OS")
    press(f, RIGHT)
    go(f, "CPU cores -2")
    press(f, "4")
    go(f, "Per VM")
    press(f, b" ")                                      # untick: one OS, common sizes; nothing invalid
    assert not f.errors() and c.values["os"] == "debian13" and c.argv(c.values)[2] == "debian13"
    go(f, "VMs per student")
    press(f, "3")
    assert not f.errors()


def test_create_too_many_students_marked_once(create):
    c, f = create()
    press(f, "150")
    assert set(f.errors()) == {"count"}


def test_create_seeding_keeps_a_wrong_list_marked(create):
    c, f = create()
    press(f, "2", DOWN, "3")
    go(f, "CPU cores")
    press(f, "2,4")                                     # two values for three VMs: marked
    assert "cores" in f.errors()
    go(f, "Per VM")
    press(f, b" ")
    assert c.values["cores1"] == "2,4" and "cores1" in f.errors()      # carried over, still marked


def test_create_a_unicode_digit_is_marked_not_a_crash(create):
    c, f = create()
    press(f, "\u00b2")
    assert f.errors() == {"count": "a number 1-99 students"}


def test_create_vms_added_later_copy_the_one_before_even_when_the_list_fits(create):
    c, f = create()
    press(f, "2", DOWN, "2")
    go(f, "CPU cores")
    press(f, "2,4,8")                                    # three values for two VMs: carried over whole
    go(f, "Per VM")
    press(f, b" ")
    go(f, "VMs per student")
    press(f, "3")
    assert c.values["cores3"] == c.values["cores2"] == "2,4,8"
