"""Cloud-free tests for synchronous Vision batching, response accuracy and errors."""
from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from vision_ocr import batch_document_text_detection


def response(text="pH 7.3", confidence=0.97, code=0, message=""):
    polygon=types.SimpleNamespace(
        vertices=[
            types.SimpleNamespace(x=10,y=20),
            types.SimpleNamespace(x=30,y=10),
            types.SimpleNamespace(x=35,y=40),
            types.SimpleNamespace(x=15,y=50),
        ]
    )
    word=types.SimpleNamespace(
        confidence=confidence,
        bounding_box=polygon,
        symbols=[types.SimpleNamespace(text="pH"),
                 types.SimpleNamespace(text=" "),
                 types.SimpleNamespace(text="7.3")],
    )
    para=types.SimpleNamespace(words=[word])
    block=types.SimpleNamespace(paragraphs=[para],bounding_box=polygon)
    page=types.SimpleNamespace(
        blocks=[block],
        property=types.SimpleNamespace(
            detected_languages=[types.SimpleNamespace(language_code="en")]
        ),
    )
    return types.SimpleNamespace(
        error=types.SimpleNamespace(code=code,message=message),
        full_text_annotation=types.SimpleNamespace(
            text=text,pages=[page] if text else [],
        ),
    )


class DummyVision:
    class Image:
        def __init__(self,content):
            self.content=content

    class Feature:
        Type=types.SimpleNamespace(DOCUMENT_TEXT_DETECTION=11)
        def __init__(self,type_):
            self.type_=type_

    class ImageContext:
        def __init__(self,language_hints):
            self.language_hints=language_hints

    class AnnotateImageRequest:
        def __init__(self,**kwargs):
            self.kwargs=kwargs


class TestBatchOCR(unittest.TestCase):
    def test_one_rpc_preserves_short_numeric_and_rotated_word_coordinates(self):
        calls=[]
        class Client:
            def batch_annotate_images(self,requests,timeout,retry):
                calls.append((requests,timeout,retry))
                return types.SimpleNamespace(
                    responses=[response(),response(
                        text="",code=3,message="Invalid image data"
                    )]
                )
        with patch("vision_ocr._vision",return_value=DummyVision):
            with patch("vision_ocr._get_client",return_value=Client()):
                result=batch_document_text_detection(
                    [b"synthetic-first",b"synthetic-second"],
                    bbox_detail="word",
                )
        self.assertEqual(len(calls),1)
        self.assertEqual(len(calls[0][0]),2)
        self.assertEqual(result[0].text,"pH 7.3")
        self.assertTrue(result[0].success)
        self.assertEqual(result[0].regions[0]["polygon"],
                         [[10,20],[30,10],[35,40],[15,50]])
        self.assertFalse(result[1].success)
        self.assertIn("Invalid image",result[1].error)

    def test_low_confidence_never_accepted_as_verified(self):
        class Client:
            def batch_annotate_images(self,requests,timeout,retry):
                return types.SimpleNamespace(responses=[
                    response(confidence=0.11)
                ])
        with patch("vision_ocr._vision",return_value=DummyVision), patch(
            "vision_ocr._get_client",return_value=Client()
        ):
            out=batch_document_text_detection(
                [b"synthetic"],min_confidence=0.5
            )
        # Even single short regulatory labels must be reviewed when
        # confidence is below the configured threshold. Original text stays.
        self.assertTrue(out[0].success)
        self.assertEqual(out[0].text,"pH 7.3")
        self.assertIn("low_confidence",out[0].error)

    def test_429_retry_is_bounded_and_quota_is_signaled(self):
        calls=[]
        class Client:
            def batch_annotate_images(self,requests,timeout,retry):
                calls.append(len(requests))
                if len(calls)==1:
                    raise RuntimeError("429 RESOURCE_EXHAUSTED")
                return types.SimpleNamespace(responses=[response()])
        class Limiter:
            def __init__(self):
                self.events=[]
            def acquire(self,n):
                self.events.append(("acquire",n))
            def cooldown(self,n):
                self.events.append(("cooldown",n))
            def success(self):
                self.events.append(("success",))
        limit=Limiter()
        with patch("vision_ocr._vision",return_value=DummyVision),patch(
            "vision_ocr._get_client",return_value=Client()
        ),patch("vision_ocr.time.sleep",return_value=None):
            result=batch_document_text_detection(
                [b"synthetic"],limiter=limit,max_retries=2,
            )
        self.assertTrue(result[0].success)
        self.assertEqual(calls,[1,1])
        self.assertEqual(
            [e[0] for e in limit.events],
            ["acquire","cooldown","acquire","success"],
        )

    def test_limits_are_rejected_before_calls(self):
        with self.assertRaisesRegex(ValueError,"1..16"):
            batch_document_text_detection([b"img"]*17)
        with self.assertRaisesRegex(ValueError,"20MB"):
            batch_document_text_detection([b"a"*(20*1024*1024+1)])


if __name__=="__main__":
    unittest.main()
