# samples

两段测试音频，中文夹英文（16 kHz / 单声道 / 16 bit PCM）。

来源：[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) 的模型发布包
`sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20/test_wavs/`，
Apache-2.0 许可。

直接试：

```sh
python3 dictation.py --file samples/1.wav
python3 dictation.py --file samples/1.wav --realtime   # 按 1 倍速喂，模拟实时听写
```
