*jarvis but cooler*

use the ui zip i include for how you should do the app functionally
# some major settings
*all 'model' options are primarily for different levels of resource usage to achieve different levels of accuracy. you can download any models online, i don't really care if you make them or not*
- Wake Word Model
	- Togglable aswell. If Summary is on, this is off.
	- This constantly listens for all of your Agents call names, and then, if the probability of you having said it is above a certain percent (customizable per agent with a global default and a global percent option on by default), it proceeds.
- STT model 
	- The model that translates audio to text.
	- Should exclusively be local AIs, and probably have none downloaded by default but allow the user to choose from a wide variety (though not too many that it's redundant)
	- Large models can be unloaded when certain specified apps are open, and reloaded when the app(s) is closed.
		- The indicator comes on screen, flashes red, and then disappears if you try to use it in this case. If you do so, it queues it up for when it is next available.
- Intention Processing Model
	- This is the AI model that takes the text from the STT model, and then calls whatever function it thinks most closely matches the users intention, with whatever arguments fit.
	- Large models can be unloaded when certain specified apps are open, and reloaded when the app(s) is closed.
- TTS model
	- Not sure if TTS models even exist or if it would be an important distinction.
	- Large models can be unloaded when certain specified apps are open, and reloaded when the app(s) is closed.
- TTS voice
	- Try to include a reasonable amount, and try to make sure they don't sound extremely weird.
	- Can be changed per agent.
- Agents
	- This allows you to set up specific agents who each have specific functions enabled/disabled. For example, here are some use cases:
		- Afterglow, clip that.
		- Claude, What is the meaning of life?
		- Jeeves, open all my apps.
	- Each agent can get toggles for what they listen to; as in just the user, just the desktop audio, or both.
		- Aswell as this, the agent can go through your microphone, speakers, or both. Customizable.
	- Agents can be toggled enabled/disabled based on certain apps being open or focused.
	- Can override what models are used for all of the above model lists.
	- Can have a 'default prompt', which lets you give them a personality and such.
	- Multiple agents can be active at a time.
- Logged in accounts (allows you to pair other apps, e.g. claude, chatgpt, grok, gemini, and have them take requests from the app and give outputs)
	- For GPT and Gemini, you have to manually add them via codex cli or gemini api keys.
	- For Claude and Grok, this is logging you into the 'jeeves browser', which will then open whenever you try to use Claude or Grok.
- Onscreen indicator toggles for:
	- STT activated (maybe a mic in a customizable corner)
	- STT output (probably in the same spot as STT activated, right under it)
	- Intention Processing (probably a loading icon, color customizable per agent)
		- Thinking (color change to a customizable color, gray by default)
			- Clicking on this will show you the current thoughts of the agent.
		- Researching (color change to a customizable color, blue by default)
			- Clicking on this will show you the current thoughts of the agent, aswell as whatever they are looking at.
			- This will activate when it is looking up through Wikpedia.
		- Responding (color change to a customizable color, black with a white outline by default)
			- Clicking on this will toggle pause the response.
		- Asking for User Input
			- Flashes white.
		- If it is unable to guess (indicated by purple by default, customizable) what you are saying, it will ask a clarifying question via Local Response.
- 'Manual Request'
	- two togglable keybinds (not mutually exclusive)
		- 'Text Request'
			- allows you to type exactly what you want to the agent. defaults to not picking an agent, so you would need to include the agents name that you wish to use.
		- 'Voice Request'
			- manually activates the STT of a specified model (separate keybinds for each)
		- can also use commands instead of keybinds, jeeves --manual_request=voice --agent="idk"
- 'Manual Response Review'
	- opens a pop-up (doesnt need the app open) that shows you the most recent response, and has left/right arrows to navigate to previous responses. include everything the AI did following it aswell.
- Time to wait after user has finished talking to stop listening
	- The user can also click the microphone onscreen to add 5 seconds, and holding it keeps it on indefinitely until you let go (plus 1 second).
- 'Abort' key
	- Makes all agents stop.
	- Makes all control mode inputs release.
- Local AI Model for Full Responses
	- Only used in the 'Local Response' function.
- Wikipedia Download/Update
	- Allows the user to download all of Wikipedia (no pictures) for the agents to reference. You can redo this download at any time to make sure your information is up to date. Maybe a 'Smart Update' option where it only does the changes, to reduce download time and bandwidth usage?
- Training the Agents
	- You can manually train the STT agents on your voice if you want them to be more accurate, aswell as manually training the intention guesser if you want it to be more accurate.
# Functions
One of the most major features of Jeeves. The intention processing is done through a dictionary explaining all of the following functions, aswell as their arguments. The user can 'Export' any custom functions they make.
#### Normal Functions
What actually gets ran by the AIs. Users can create new full functions by combining partial functions together.
*note: users can change keywords of all functions to whatever they want. default is listed next to each function*
- Summary (OFF)
	- if enabled (needs to be manually toggled), will keep a log file (with desktop audio and users microphone being distinct, and timestamps included) of the last customizable amount of time (1 hour default)
- Extended Prompt Mode (ON)
	- makes the AI wait until the user says 'End extended prompt mode' to finish
- Online Prompt Mode: Agent ------ (OFF)
	- Fill in 'Agent' with whatever online AI you want to handle your request. 
		- Codex CLI is used for GPT
		- Gemini uses a free API key
		- Claude and Grok require the user to click on the indicator when it is flashing white, which will then open the browser with the request already placed in there. The user will have to send it manually (by clicking the send button), and then copy it for Jeeves to be able to read it (by clicking the copy button).
	- Use MCP if supported?
- Control Mode (OFF)
	- Allows the Agent to provide inputs and outputs via virtual devices (jeeves-keyboard & jeeves-mouse & jeeves-controller)
		- Maybe interface with puppetry, or just copy the puppetry code? I'll include the zip so you can decide.
		- Supports mouse movement, mouse buttons (up/down), keyboard buttons (up/down).
- Local Response (ON)
	- Allows a Local AI to run based on the included string. The model for this is also customizable in settings.
	- If enabled, this is the default function.
- Macros (OFF)
	- Macro creation makes a macro to do something you want, via Puppetry.
	- Also supports Macro Adjustment.
	- Both of the above use the Local Response model in conjunction with the Puppetry Dictionary to provide their changes.
	- Can also run macros with customizable arguments.
- Timers/Schedules (ON)
	- Creates an onscreen indicator in the bottom right (togglable)
- Handoff (ON)
	- Allows one agent to send info to another agent. The sending agent will get a list of the receiving agent's enabled functions, and then tell them any relevant info.
		- This only happens if the user makes it clear that they want it to; e.g. "Jeeves, with what we've been talking about, ask Claude to give his thoughts on it."
		- You can choose, per agent, which agents can be spoken to.
#### Partial Functions
These are used to compose functions. Users can also create new partial functions in python code.
-  Run Command (with output support, as in you could do something like "puppetry --list" and get the list returned properly)
	- A setting where, prior to running any command, Jeeves will show you what it will run, followed by you needing to click the onscreen indicator showing its current progress, OR say 'Proceed.' (changeable keyword)
		- Include a 'trusted command list'. If no arguments are included with the trusted command, it assumes all arguments are fine.
	- shell metacharacters (e.g. ; && | $() are all rejected)
- Screen Reading
	- Takes an argument of something to look for on the screen, and then looks for it, and then outputs the absolute position of it in pixels.
	- Can also, as a second argument, provide the 'bounds' of an object— defaults to just a rectangle, but can also have more than 4 points for more precision.
	- Reading text onscreen. Can be bounded vaguely, e.g. "in the middle" (if no text is found in a case like this, expand until text is found).
- Get Current Mouse Position
- Get Currently Held Keys
- Get Currently Focused App
- Get a list of Currently Open Apps
- Get ----- App Position
- Get ----- App Workspace Number
- Get ----- App Size
- Memory
	- Allows you to reference the most recent X (customizable, 3 default) requests. This can be used for things like 'Puppetry, adjust the macro I just made.'
	- Also has a 'long term memory' option, which lets you commit something to your RAM rather than just the last 3 requests.
		- Done by including something like 'Remember for a while' or 'Commit to long term memory'
		- Include a 'permanent memory' option, which lets you commit something to your storage.
			- Done by including something like 'Remember forever' or 'Commit to permanent memory'.
- Mark Screen Position
	- Can circle anywhere on your screen with a customizable radius.
- Clipboard support
	- Get current copied info
	- Set current copied info
- Basic control flow (e.g. if, loops, when, conditionals, blablabla)
- Event triggers
	- e.g. X app focused, X process started, X text appears on screen, X time happens, X file changed, keyword in audio (mic or desktop, both toggleable), etc.
- Play sound
- Speak
- Ask user
- Wait/delay
- Send notification
- Request a website (maybe open it in a hidden window)
- Read specific files
- Create specific files
- Run specific files

# Integration with other apps/systems
Simple category, but there should be some system where apps can add their own functions to Jeeves.
Also have declarative settings support in NixOS.
	Any settings declared in NixOS will not be changeable in app, and will be 'locked'.
Imported functions should show up in the app as a pop-up, allowing the user to add/deny the imported functions, as well as what default functions they use.

Ensure that the app runs a daemon and the GUI separately, the daemon handling all of the actual work, and the GUI just being a front-end with the settings and such, sending a request to the daemon to change anything.
# The Dictionary
Keeps a explanation of every function and the most common arguments for it. Every function must explicitly define itself, how it works, what arguments can be passed for specific things (e.g. function variations), and what types of arguments can be passed for non-specific things (e.g. app names). Also have 'blocked statements' for each one that can be manually inputted.
# Misc Features
#### Dry Run
There is a console in the app that lets you see what WOULD happen if you were to say something.
#### Rating Responses
You can go to a history area and rate each response manually, and potentially include a comment to help the agents know why it was good/bad. This would be included next to what functions the agents used, and what functions the agents should have used according to you, in the dictionary.
