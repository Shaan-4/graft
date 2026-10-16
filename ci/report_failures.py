"""Turn failing tests in a JUnit XML report into GitHub annotations, so each
failure shows up on the run's summary page without opening the raw log."""
import sys
import xml.etree.ElementTree as ET

path = sys.argv[1] if len(sys.argv) > 1 else "report.xml"
try:
    root = ET.parse(path).getroot()
except (OSError, ET.ParseError) as e:
    print(f"::error::no test report ({e})")
    sys.exit(0)

for case in root.iter("testcase"):
    for bad in list(case.findall("failure")) + list(case.findall("error")):
        name = f"{case.get('classname')}.{case.get('name')}"
        detail = (bad.get("message") or "") + "\n" + (bad.text or "")
        detail = detail.strip()[-1500:]
        # Workflow commands are one line: encode newlines as %0A.
        detail = detail.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
        print(f"::error title={name}::{detail}")
