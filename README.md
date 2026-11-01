# ESP32 Remote Input

메인 컴퓨터의 창에서 ESP32-C3와 Bluetooth HID를 통해 다른 기기의 입력을 제어합니다. Slave에 앱을 설치할 필요가 없습니다. 한 번에 한 기기를 지원하며 iPad에서는 AssistiveTouch가 필요합니다.

## 준비

- ESP32-C3 Super Mini와 데이터 통신이 가능한 USB 케이블
- 메인 컴퓨터의 Python 3, Tkinter, pyserial, 펌웨어 업로드용 PlatformIO CLI
- iPad에서 설정 → 손쉬운 사용 → 터치 → AssistiveTouch 켜기

ESP32를 연결한 후 펌웨어를 빌드하고 업로드합니다.

```sh
./firmware/program.sh
```

포트 자동 선택이 잘못되면 `./firmware/program.sh --upload-port /dev/cu.usbmodem2101`처럼 포트를 지정합니다. Windows에서는 PlatformIO CLI로 `pio run -d firmware -t upload`을 실행합니다. 펌웨어를 바꾸면 기존 보드 펌웨어가 덮어써집니다.

Slave의 Bluetooth 설정에서 `ESP32 Remote Input`을 페어링합니다. iPad의 경우 AssistiveTouch의 기기 메뉴에서 연결합니다. 기존 HID 구성을 바꾼 펌웨어라면 iPad에서 해당 기기를 지우고 다시 페어링해야 할 수 있습니다.

## 앱 실행

### 브라우저 버전

Node.js와 pyserial이 설치된 Python 환경이 필요합니다. 아래 명령으로 같은 컴퓨터에서만 접속 가능한 로컬 서버를 실행한 다음, `http://127.0.0.1:8765/`를 엽니다.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r desktop/requirements.txt
node web/server.js
```

Windows에서는 `.venv\\Scripts\\python.exe -m pip install -r desktop/requirements.txt`를 사용합니다. 다른 Python을 사용하려면 `REMOTE_INPUT_PYTHON` 환경 변수에 실행 파일 경로를 지정합니다. 브라우저 화면에서 설정을 열어 ESP32 포트를 선택하고 연결합니다. 기존 데스크톱 앱과 동시에 같은 포트를 사용할 수 없습니다. 브라우저나 운영체제가 선점하는 단축키는 전달되지 않을 수 있습니다. 키 입력과 포인터 동작은 브라우저 및 운영체제에 따라 차이가 있을 수 있습니다.

### Python 데스크톱 버전

Tkinter가 포함된 Python으로 가상 환경을 만들고 의존성을 설치합니다.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r desktop/requirements.txt
.venv/bin/python desktop/app.py
```

Windows에서는 `.venv\Scripts\python.exe`를 사용합니다. Linux에서 Tkinter 모듈이 없으면 배포판의 Python Tk 패키지가 필요합니다. macOS에서는 사용 중인 Python이 Tkinter를 포함하는지 `python3 -c "import tkinter; print(tkinter.TkVersion)"`로 확인하세요. 오래된 시스템 Tk에서 화면이 표시되지 않을 수 있습니다. Homebrew Python 3.14를 사용한다면 `brew install python-tk@3.14` 후 `/opt/homebrew/bin/python3.14 -m venv .venv`로 가상 환경을 만들 수 있습니다.

앱의 **설정** 창에서 ESP32 포트를 선택해 연결하고, 대상 기기의 가로·세로 해상도를 입력해 적용합니다. 제어 영역에 포커스를 주면 커서 이동, 좌우 클릭, 드래그와 세로 스크롤이 Slave에 전달됩니다. 첫 클릭은 앱에 포커스를 주는 데 사용될 수 있습니다. 영역에 포커스가 있고 커서가 들어와 있을 때 키보드 입력이 전달됩니다. 영역을 벗어나거나 포커스를 잃으면 눌린 입력을 해제합니다. 여러 Slave 자동 전환은 아직 지원하지 않습니다.

가운데 버튼과 뒤로/앞으로(4/5번) 버튼, Shift+휠 또는 트랙패드의 가로 스크롤도 전달됩니다. 대상 기기가 고해상도 휠(Resolution Multiplier)을 켜면 스크롤이 1/8 단위로 부드럽게 전달되고, 지원하지 않는 기기에는 한 칸 단위로 모아서 전달됩니다. 상단의 **미디어** 메뉴로 재생·볼륨·밝기 키, Android 홈/뒤로, 시스템 절전을 보낼 수 있습니다. 이 기능들은 HID 구성을 바꾸므로, 이전 펌웨어로 페어링했던 기기에서는 이 기기를 지우고 다시 페어링해야 합니다.

Bluetooth 연결이 끊겼다면 ESP32는 광고 재개를 자동으로 시도합니다. 설정에서 USB 포트를 연결한 뒤 **Bluetooth 복구**를 누르면 광고를 다시 시작할 수 있습니다. 이미 연결된 기기는 끊지 않으며 페어링 정보도 삭제하지 않습니다. 실제 재연결은 대상 기기의 연결 요청에 달려 있습니다. 앱은 연결 상태 변화와 펌웨어의 `BLE ...` 진단 줄을 macOS에서는 `~/Library/Logs/esp32_remote_mouse.log`, 그 외에는 `~/.esp32_remote_mouse.log`에 기록합니다. `REMOTE_INPUT_LOG` 환경 변수로 경로를 바꿀 수 있습니다.

설정의 **자동 절전**에 지정한 시간(분, 기본 5분) 동안 제어 영역에서 커서 이동·클릭·스크롤·키보드 입력이 없으면 앱이 ESP32에 절전을 요청합니다. 커서가 제어 영역 밖에 있거나 앱에 포커스가 없는 시간도 유휴 시간으로 계산합니다. ESP32는 대상 기기와의 Bluetooth 연결을 끊고 광고를 멈춘 뒤 CPU 클럭을 낮춥니다. 포커스가 있는 제어 영역에서 다시 조작하거나 상단 버튼을 누르면 광고를 재개하고, 대상 기기가 다시 연결하면 입력이 전달됩니다. 재연결 전까지의 입력은 전달되지 않습니다. 0을 입력하면 자동 절전을 사용하지 않으며, 적용한 시간은 macOS에서는 `~/Library/Application Support/esp32_remote_mouse/settings.json`, 그 외에는 `~/.esp32_remote_mouse.json`에 저장되어 다음 실행에도 유지됩니다. `REMOTE_INPUT_SETTINGS` 환경 변수로 경로를 바꿀 수 있습니다. USB 시리얼 연결을 유지해야 다시 깨울 수 있으므로 ESP32-C3의 딥 슬립은 사용하지 않습니다.

대상 기기가 연결되면 ESP32가 그 기기의 이름과 제조사·모델 정보(제공하는 경우)를 읽어 앱 상단 상태와 창 제목에 `연결됨: 기기 이름 (모델)` 형태로 표시합니다. 시리얼 `peer` 명령으로도 확인할 수 있습니다. 표시되는 이름은 대상 기기가 블루투스에 알려주는 이름입니다.

iPad 같은 대상 기기는 주소를 주기적으로 바꾸므로, ESP32가 페어링 때 대상의 신원 키(IRK)를 저장해야 재연결됩니다. 시리얼 `bonds` 명령에서 `irk=0`인 본딩이 보이면 `unpair`로 본딩을 지우고, 대상 기기에서 이 기기를 지운 뒤 다시 페어링합니다.

상단 가장자리에 커서를 올리면 버튼이 표시됩니다. **전체 화면** 버튼으로 전환하면 설정한 대상 화면 비율을 유지하면서 제어 영역이 화면 안에서 최대 크기로 표시됩니다. 전체 화면을 종료하려면 상단 메뉴에서 **전체 화면 종료**를 누릅니다.

설정의 **영상 캡처**에서 캡처 카드 또는 카메라를 선택하면 입력 영상이 비율을 유지하며 제어 영역에 표시됩니다. 이때 수동 해상도 설정은 비활성화되고 영상 표시 영역에 맞춰 좌표가 전달됩니다. **사용 안 함**을 선택하면 이전 수동 해상도로 돌아갑니다. FFmpeg가 PATH에 설치되어 있어야 하며, macOS에서는 영상 장치 접근 권한이 필요할 수 있습니다. 영상이 끊기거나 창 크기를 바꾸는 동안은 입력 전달을 일시 중지합니다. 캡처 카드의 신호에 별도 검은 여백이 포함되어 있다면 그 여백도 영상 영역에 포함됩니다.

캡처 장치가 해상도 목록을 제공하면 **캡처 해상도**에서 입력 해상도를 선택할 수 있습니다. **자동**은 장치의 기본 설정을 사용하며, 선택한 입력 해상도와 관계없이 표시 크기는 창 크기에 맞춰 조정됩니다.

macOS에서 장치가 프레임 속도를 보고하면 **FPS**에서도 캡처 속도를 선택할 수 있습니다. 목록은 FFmpeg가 장치별로 보고하는 값에 따라 달라지며, Windows와 Linux에서는 현재 자동 설정을 사용합니다. 화면 미리보기는 최대 30 FPS이고 설정 창이 열려 있는 동안에는 약 5 FPS로 갱신됩니다.

상단 **홈** 버튼은 `Command + H`를, **앱 전환** 버튼은 `Command + Tab`을 보냅니다. 앱 전환 버튼은 직전에 사용한 앱으로 전환하며 앱 전환기 화면을 열지는 않습니다. iPad에서 전체 키보드 접근을 켜면 한글 입력에 영향을 줄 수 있습니다.

제어 영역이 활성화된 동안 macOS의 `Command + H`는 iPad에 전달되며 Mac 앱 숨기기를 막습니다. Windows와 Linux에서는 `Control + H`를 iPad의 `Command + H`로 변환합니다. 제어 영역을 벗어나면 각 운영체제의 원래 동작으로 돌아갑니다.

**Spotlight** 버튼은 `Command + Space`, **Dock** 버튼은 `Command + Option + D`를 보냅니다. Dock 단축키는 iPadOS 버전과 설정에 따라 동작이 다를 수 있습니다.

**앱 창 보기**는 실험적으로 `Globe + ↓`를 보냅니다. 현재 앱의 열린 창을 표시하는 기능이며 iPadOS 버전과 기기에 따라 동작이 다를 수 있습니다. Globe HID를 추가한 펌웨어를 올린 뒤에는 기존 Bluetooth 기기를 지우고 다시 페어링해야 합니다.

기본적으로 상대 마우스 채널로 버튼을 전송합니다. iPad에서 클릭 동작을 확인했으며, 설정의 **상대 마우스로 클릭**을 끄면 절대 포인터 채널로 버튼을 전송합니다. 클릭 위치와 드래그는 기기에 따라 다를 수 있습니다.

Caps Lock은 기본적으로 물리 키 그대로 전달합니다. 설정에서 iPad의 입력 언어 전환 단축키 `Control + Space`로 바꾸는 실험적 옵션을 사용할 수 있습니다. iPad에 등록된 입력 언어가 여러 개면 순서대로 전환됩니다.

현재 iPad 1대에서 BLE 키보드, 절대 포인터 이동과 그리기 드래그를 확인했습니다. 데스크톱 통합과 다른 OS·기기 조합의 동작은 별도 확인이 필요합니다. macOS Tk의 입력기 이벤트에서는 실제 물리 키 정보가 누락될 수 있어 키보드 배열이나 입력기에 따라 물리 키가 정확하게 전달되지 않을 수 있습니다. iPad의 AssistiveTouch 포인터는 실제 손가락 터치와 동작이 다를 수 있습니다.
