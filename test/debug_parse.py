import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from src.core.unified_writer import UnifiedWriter
import json

parse = UnifiedWriter._parse_response

# Debug: test the DeepSeek style response
raw = json.dumps({
    "polished_text": "animal pigmentation is important.",
    "citation_notes": [{"marker": "Prakash et al., 2024", "status": "accurate", "note": "OK"}],
    "supervisor_notes": [],
}, ensure_ascii=False)

print("Test 1 - pure JSON:", "PASS" if parse(raw) else "FAIL")

# Test 2: JSON with Chinese prefix text
raw2 = "Here is the result:\n" + raw
print("Test 2 - prefix+JSON:", "PASS" if parse(raw2) else "FAIL")

# Test 3: DeepSeek actual style
raw3 = "OK, let me polish:\n\n" + raw + "\n\nHope helpful."
print("Test 3 - wrapped JSON:", "PASS" if parse(raw3) else "FAIL")

# Test 4: Plain text, no JSON
raw4 = "动物体表的色素沉积与图案形成是适应性演化中最为直观的表型。"
r = parse(raw4)
print("Test 4 - plain text:", "PASS" if r else "FAIL")

# Test 5: JSON with real newlines in string (illegal JSON)
raw5 = '{\n  "polished_text": "line1\n\nline2",\n  "citation_notes": [],\n  "supervisor_notes": []\n}'
r = parse(raw5)
print("Test 5 - unescaped newlines:", "PASS" if r else "FAIL")

# Test 6: Test scenario from test file input
test_text = (PROJECT_ROOT / "test" / "写作-test.txt").read_text(encoding="utf-8")
first_para = test_text.strip().split("\n\n")[0]

raw6 = json.dumps({
    "polished_text": first_para,
    "citation_notes": [],
    "supervisor_notes": [],
}, ensure_ascii=False)

r = parse(raw6)
print("Test 6 - real text as JSON:", "PASS" if r else "FAIL")

# Test 7: DeepSeek with real text  
raw7 = "Here is the polished version:\n\n" + raw6 + "\n\nChanges: improved flow."
r = parse(raw7)
print("Test 7 - real DeepSeek style:", "PASS" if r else "FAIL")
if not r:
    print("  Trying to understand why...")
    first = raw7.find('{')
    last = raw7.rfind('}')
    print(f"  First {{ at {first}, last }} at {last}")
    if first >= 0 and last > first:
        json_str = raw7[first:last+1]
        print(f"  JSON substr len={len(json_str)}")
        try:
            json.loads(json_str)
            print("  Parses OK!")
        except json.JSONDecodeError as e:
            print(f"  Parse error: {e}")
            # Show position around error
            pos = e.pos
            print(f"  Error context: ...{json_str[max(0,pos-30):pos+30]}...")
