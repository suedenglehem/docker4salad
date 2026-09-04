curl https://pasta-potato-9u54c16bga1zftr1.salad.cloud/v1/chat/completions \
  -X POST \
  -H 'Content-Type: application/json' \
  -H 'Salad-Api-Key: salad_cloud_user_u1se6JaP4k7eZQXtGrosOWmEE5BJ9YoF5LxAstPwglE31XvGE' \
  -d '{
    "model": "qwen",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "Explain how quantization helps GGUF models."}
    ],
    "max_tokens": 12800,
    "temperature":0
  }'
