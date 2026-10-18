import unittest
from agent.export import to_markdown
from agent.models import Brief, Draft, Evidence, ImageRef, ImagePlacement, Paragraph, Session


class ExportImageTest(unittest.TestCase):
    def session(self, model='MateBook 14'):
        return Session(brief=Brief(product='MateBook 14'),
            evidence=Evidence(images=[ImageRef('I1', model, 'photo.png', 'photo', 'images/photo.png', origin_url='https://example.com/photo.png')]),
            draft=Draft(intro='导语测试', body=[Paragraph('正文测试')], images=[ImagePlacement('I1', 0, '图注测试')]))

    def test_markdown_contains_actual_image_and_source(self):
        md = to_markdown(self.session())
        self.assertIn('![配图 I1](<https://example.com/photo.png>)', md)
        self.assertIn('images/photo.png', md)
        self.assertLess(md.index('导语测试'), md.index('![配图'))
        self.assertLess(md.index('![配图'), md.index('正文测试'))

    def test_web_uses_local_image_resolver(self):
        md = to_markdown(self.session(), image_url=lambda im: '/gradio_api/file=/image.png')
        self.assertIn('![配图 I1](</gradio_api/file=/image.png>)', md)

    def test_unavailable_or_wrong_model_image_is_not_embedded(self):
        self.assertNotIn('![', to_markdown(self.session(), image_url=lambda im: ''))
        self.assertNotIn('![', to_markdown(self.session('MateBook X Pro')))
