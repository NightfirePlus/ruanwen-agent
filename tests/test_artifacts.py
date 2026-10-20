import tempfile
import unittest
from pathlib import Path

from agent import artifacts, export
from agent.models import (
    Brief, Draft, Evidence, Fact, ImagePlacement, ImageRef, Issue, Paragraph, Review, Session, Strategy,
)


def sample_session():
    return Session(
        brief=Brief(
            product="MateBook 14", audience="大学新生", scene="开学季", platform="小红书",
            goal="种草", main_problem="携带与学习",
        ),
        evidence=Evidence(facts=[Fact("F1", "MateBook 14", "重量约1.36kg", "产品事实.md", "P1/MateBook 14/产品事实.md")]),
        strategy=Strategy(scene={"angle": "围绕学习与携带"}, platform={"name": "小红书"}),
        draft=Draft(
            title="开学轻装上阵", intro="从每天背电脑去教室说起。",
            body=[Paragraph("机身重量约1.36kg。", ["F1"])], cta="欢迎到店体验。",
            tags=["开学季", "华为电脑"],
        ),
        review=Review([Issue("fact", "llm", "warn", "正文", "发布前确认实际配置")]),
    )


class ArtifactExportTest(unittest.TestCase):
    def test_article_markdown_excludes_report_sections(self):
        text = export.to_article_markdown(sample_session())
        self.assertIn("开学轻装上阵", text)
        self.assertNotIn("知识库依据", text)
        self.assertNotIn("核验结果", text)

    def test_docx_and_pdf_are_created_for_both_scopes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for full, prefix in ((False, "publish"), (True, "report")):
                docx = artifacts.create_docx(sample_session(), root / f"{prefix}.docx", full_report=full)
                pdf = artifacts.create_pdf(sample_session(), root / f"{prefix}.pdf", full_report=full)
                self.assertGreater(docx.stat().st_size, 1_000)
                self.assertGreater(pdf.stat().st_size, 1_000)

    def test_local_product_image_is_embedded(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            picture = root / "product.png"
            Image.new("RGB", (320, 180), "#D9162F").save(picture)
            session = sample_session()
            session.evidence.images = [
                ImageRef("I1", "MateBook 14", "product.png", str(picture), "P1/images/product.png")
            ]
            session.draft.images = [ImagePlacement("I1", 0, "产品主图")]
            docx = artifacts.create_docx(session, root / "with-image.docx")
            pdf = artifacts.create_pdf(session, root / "with-image.pdf")
            self.assertGreater(docx.stat().st_size, 2_000)
            self.assertGreater(pdf.stat().st_size, 2_000)


if __name__ == "__main__":
    unittest.main()
