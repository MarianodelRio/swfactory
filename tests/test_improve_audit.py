import pytest

from swfactory import improve


def skill(base, name, skill_md, references=None):
    folder = base / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(skill_md)
    for ref_name, content in (references or {}).items():
        refs = folder / "references"
        refs.mkdir(parents=True, exist_ok=True)
        (refs / ref_name).write_text(content)
    return folder


def frontmatter(description):
    return f"---\nname: x\ndescription: {description}\n---\n"


@pytest.fixture
def base(tmp_path):
    return tmp_path / "skills"


def test_clean_skills_have_no_findings(base):
    skill(base, "factory-a",
          frontmatter("Does A things, used for /factory-a and related work.") + "# /factory-a\n\nDoes a thing.\n")
    skill(base, "factory-b",
          frontmatter("Does B things, used for /factory-b and related work.") + "# /factory-b\n\nDoes another thing.\n")
    assert improve.audit(base)["findings"] == []


def test_oversized_skill_md_is_flagged(base):
    skill(base, "factory-a", frontmatter("Does A things, used for /factory-a and related work.")
                            + "# /factory-a\n" + "line\n" * 121)
    findings = improve.audit(base)["findings"]
    assert [f["check"] for f in findings] == ["skill_too_large"]
    assert findings[0]["file"] == "factory-a/SKILL.md"
    assert "126" in findings[0]["detail"]


def test_oversized_reference_is_flagged(base):
    skill(base, "factory-a", frontmatter("Does A things, used for /factory-a and related work."),
         references={"big.md": "line\n" * 301})
    findings = improve.audit(base)["findings"]
    assert [f["check"] for f in findings] == ["skill_too_large"]
    assert findings[0]["file"] == "factory-a/references/big.md"


def test_duplicated_instruction_across_skills_is_flagged(base):
    shared = "Change Factory state only through the swfactory CLI with --json always.\n"
    skill(base, "factory-a", frontmatter("Does A things, used for /factory-a and related work.") + shared)
    skill(base, "factory-b", frontmatter("Does B things, used for /factory-b and related work.") + shared)
    findings = improve.audit(base)["findings"]
    assert [f["check"] for f in findings] == ["duplicated_instruction"]
    assert findings[0]["file"] == "factory-b/SKILL.md"


def test_unconditional_reference_in_description_is_flagged(base):
    skill(base, "factory-a", frontmatter("Does A things; see references/extra.md for more, used for /factory-a."),
         references={"extra.md": "# extra\n"})
    findings = improve.audit(base)["findings"]
    assert [f["check"] for f in findings] == ["unconditional_reference"]
    assert findings[0]["file"] == "factory-a/SKILL.md"
