curl https://tuna-rutabaga-bpidycs7jokowkbc.salad.cloud/v1/chat/completions \
  -X POST \
  -H 'Content-Type: application/json' \
  -H 'Salad-Api-Key: salad_cloud_user_u1se6JaP4k7eZQXtGrosOWmEE5BJ9YoF5LxAstPwglE31XvGE' \
  -d '{
    "model": "llama-cpp",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "Explain how quantization helps GGUF models."}
    ],
    "max_tokens": 128,
    "temperature":0
  }'
