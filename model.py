import google.generativeai as genai

genai.configure(api_key="AIzaSyARO-g5Vp4ykviKEx_i4lECAiyDXhU_al8")

model = genai.GenerativeModel("gemini-2.0-flash-live-001")

chat = model.start_chat()

response = chat.send_message("Write a summary about AI.")
print(response.text)
