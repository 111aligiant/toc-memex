# Test fixtures

本目录默认**不入库**（见根目录 `.gitignore`）。需要跑完整 OCR 回归测试时，把本地样本放到：

```
tests/fixtures/paddleocr_sample.jsonl
tests/fixtures/ocr/<doc_id>/raw.jsonl
tests/fixtures/ocr/<doc_id>/bookmarks.json
```

缺失时，依赖这些样本的测试会自动 skip，其余单元测试照常运行。

样本不要包含个人隐私、token、签名 URL 或未脱敏的原始文档。