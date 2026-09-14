First pass at deploy of custom dockers on salad. You have everything prepared for cline (or other harness) to proceed. 
I use this for deploying my llama-cpp on salad on demand, switching cards and quantities of cards. Today cline does all the work for me. 
Docker image is a git cloned llama-server compiled with cuda sdk 12.8 (=> it can run on 3090 & 5090). 
