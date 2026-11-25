"""DOCX extraction fixtures, independent source counts, and adversarial input."""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from docx_evidence import capture,verify,read_evidence,_folder,W,R,A,C,P

CONFIG=json.loads((Path(__file__).resolve().parents[1]/"profiles/docx_evidence_example.json").read_text())
PNG=b"\x89PNG\r\n\x1a\nsynthetic asset"


def fixture(path,document=None,extra=None):
    if document is None:
        document=f'''<w:document xmlns:w="{W}" xmlns:r="{R}" xmlns:a="{A}" xmlns:c="{C}">
        <w:body><w:p><w:r><w:t>剂量 20 mg</w:t><w:tab/><w:t>Result</w:t></w:r></w:p>
        <w:tbl><w:tblGrid><w:gridCol w:w="100"/><w:gridCol w:w="100"/></w:tblGrid>
        <w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge w:val="restart"/></w:tcPr>
        <w:p><w:r><w:t>Header</w:t></w:r></w:p></w:tc></w:tr>
        <w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge/></w:tcPr>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Nested</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        <w:p/></w:tc></w:tr></w:tbl>
        <w:p><w:r><w:drawing><a:blip r:embed="rId1"/><a:blip r:embed="rId2"/>
        <c:chart r:id="rId3"/></w:drawing></w:r></w:p>
        <w:p><w:hyperlink r:id="rId4"><w:r><w:t>Link</w:t></w:r></w:hyperlink></w:p>
        </w:body></w:document>'''
    with zipfile.ZipFile(path,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml","<Types/>")
        z.writestr("word/document.xml",document)
        z.writestr("word/header1.xml",f'<w:hdr xmlns:w="{W}"><w:p><w:r><w:t>Header note</w:t></w:r></w:p></w:hdr>')
        z.writestr("word/media/image1.png",PNG)
        z.writestr("word/media/image2.png",PNG)
        z.writestr("word/charts/chart1.xml",f'<c:chartSpace xmlns:c="{C}"><c:numCache><c:pt idx="0"><c:v>0.04</c:v></c:pt></c:numCache></c:chartSpace>')
        z.writestr("word/_rels/document.xml.rels",f'<Relationships xmlns="{P}"><Relationship Id="rId1" Type="{R}/image" Target="media/image1.png"/><Relationship Id="rId2" Type="{R}/image" Target="media/image2.png"/><Relationship Id="rId3" Type="{R}/chart" Target="charts/chart1.xml"/><Relationship Id="rId4" Type="{R}/hyperlink" TargetMode="External" Target="https://example.invalid"/></Relationships>')
        for name,data in (extra or {}).items():
            z.writestr(name,data)


class DocxEvidenceTests(unittest.TestCase):
    def test_structure_replay_and_source_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            src=root/"source.docx"
            fixture(src)
            kw=dict(policy_id="test",authorized_derivatives=True,config=CONFIG)
            out=root/"out"
            cap=capture(src,out,**kw)
            cid=cap["capture_id"]
            self.assertEqual(cap["counts"]["w:tbl"],2)
            self.assertEqual(cap["counts"]["w:tc"],3)
            self.assertEqual(cap["counts"]["w:gridSpan"],2)
            self.assertEqual(cap["counts"]["w:vMerge"],2)
            self.assertEqual(cap["asset_parts"],2)
            self.assertEqual(cap["unique_assets"],1)
            self.assertEqual(cap["chart_parts"],1)
            self.assertEqual(cap["pagination"],"NOT_RENDERED")
            self.assertTrue(capture(src,out,**kw)["skipped_verified"])
            src.unlink()
            self.assertTrue(verify(out,cid,config=CONFIG)["local_content_verified"])
            evidence=read_evidence(out,cid,config=CONFIG)
            self.assertIn("剂量 20 mg",json.dumps(evidence,ensure_ascii=False))
            self.assertIn("0.04",json.dumps(evidence))
            self.assertTrue(any(x["reason"]=="EXTERNAL_OR_UNRESOLVED_RELATIONSHIP" for x in evidence["unresolved"]))
            self.assertFalse(list(out.rglob("*.docx")))
            self.assertFalse(list(out.rglob("*.xml")))
            asset=next((_folder(out,cid)/"assets").glob("*.bin"))
            self.assertEqual(asset.read_bytes(),PNG)
            asset.write_bytes(b"corrupted")
            with self.assertRaisesRegex(ValueError,"checksum"):
                verify(out,cid,config=CONFIG)

    def test_logical_adapter_keeps_source_cells_and_exact_text_without_pages(self):
        from docx_evidence import iter_logical_blocks,logical_summary
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            src=root/"source.docx"
            fixture(src)
            out=root/"out"
            cap=capture(src,out,policy_id="test",authorized_derivatives=True,config=CONFIG)
            cid=cap["capture_id"]
            src.unlink()
            blocks=list(iter_logical_blocks(out,cid,config=CONFIG))
            self.assertEqual(len({b["block_id"] for b in blocks}),len(blocks))
            self.assertEqual(blocks,list(iter_logical_blocks(out,cid,config=CONFIG)))
            self.assertTrue(all("page_start" not in b and "page_end" not in b for b in blocks))
            self.assertEqual(blocks[0]["locator"]["part"],"word/document.xml")
            cells=[b for b in blocks if b["kind"]=="cell"]
            self.assertEqual(len(cells),3)
            self.assertEqual(cells[0]["source_properties"][0]["children"][0]["attributes"]["w:val"],"2")
            nested=next(b for b in blocks if any(s["text"]=="Nested" for s in b["segments"]))
            self.assertEqual(sum(a["tag"]=="w:tbl" for a in nested["table_ancestry"]),2)
            first=blocks[0]
            self.assertEqual("".join(s["text"] for s in first["segments"]),"剂量 20 mg\tResult")
            self.assertEqual(sum(len(b["relationships"]) for b in blocks),4)
            self.assertEqual(logical_summary(out,cid,config=CONFIG)["native_text_tokens"],cap["counts"]["w:t"])

    def test_nested_paragraph_and_revision_text_not_duplicated(self):
        from docx_evidence import iter_logical_blocks
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            src=root/"source.docx"
            document=f'''<w:document xmlns:w="{W}"><w:body>
            <w:p><w:r><w:t>Outer</w:t><w:drawing><w:txbxContent>
            <w:p><w:r><w:t>Inner</w:t></w:r></w:p></w:txbxContent></w:drawing>
            <w:t>After</w:t></w:r><w:del><w:r><w:delText>Deleted</w:delText></w:r></w:del>
            <w:r><w:instrText>FIELD</w:instrText></w:r></w:p>
            </w:body></w:document>'''
            fixture(src,document=document)
            cap=capture(src,root/"out",policy_id="test",authorized_derivatives=True,config=CONFIG)
            src.unlink()
            blocks=list(iter_logical_blocks(root/"out",cap["capture_id"],config=CONFIG))
            main=[b for b in blocks if b["locator"]["part"]=="word/document.xml"]
            self.assertEqual(len(main),2)
            self.assertEqual([s["text"] for s in main[0]["segments"]],["Outer","After","Deleted","FIELD"])
            self.assertEqual([s["text"] for s in main[1]["segments"]],["Inner"])
            self.assertIn("w:del",main[0]["review_context"])
            self.assertIn("w:txbxContent",main[1]["review_context"])
            self.assertIn("w:instrText",main[0]["review_context"])

    def test_policy_and_unsafe_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            src=root/"source.docx"
            fixture(src,extra={"../escape":b"x"})
            with self.assertRaises(PermissionError):
                capture(src,root/"out",policy_id="test",authorized_derivatives=False,config=CONFIG)
            with self.assertRaisesRegex(ValueError,"Unsafe"):
                capture(src,root/"out",policy_id="test",authorized_derivatives=True,config=CONFIG)
            fixture(src,document='<!DOCTYPE x [<!ENTITY y "test">]><x>&y;</x>')
            with self.assertRaisesRegex(ValueError,"DTD"):
                capture(src,root/"out",policy_id="test",authorized_derivatives=True,config=CONFIG)
            fixture(src)
            with self.assertRaisesRegex(ValueError,"ZIP inventory"):
                capture(src,root/"out",policy_id="test",authorized_derivatives=True,
                        config={**CONFIG,"max_parts":1})

if __name__=="__main__":
    unittest.main()
