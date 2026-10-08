# Những vấn đề gặp trong lúc phát triển

Ghi lại ngày 2026-10-08. Mỗi dòng nói vấn đề, nguyên nhân và cách xử lý. Nhãn ở cuối dòng:

- **[Đã sửa]**: đã xử lý trong code hoặc tài liệu.
- **[Còn mở]**: chưa xử lý xong, cần làm tiếp.
- **[Cần bạn]**: cần chủ dự án làm hoặc quyết định.

## 1. Môi trường và công cụ

- Postgres cài sẵn trên máy chiếm cổng 5432, test tích hợp báo sai mật khẩu. Docker Postgres 18 được chuyển sang cổng 5433 và bản cài sẵn không bị đụng tới. **[Đã sửa]**
- PowerShell 5.1 không có `&&`. Dùng `uv run poe check` thay vì nối lệnh. **[Đã sửa]**
- `Set-Content -Encoding utf8` ghi thêm BOM, và một lệnh `-replace` ghi nhầm ký tự `\n` thật vào `registry.py` gây lỗi cú pháp. Sửa lại bằng công cụ sửa file, `ruff format` bỏ BOM và đã quét không còn BOM nào. **[Đã sửa]**
- Ống `pyright --outputjson | python` trong PowerShell thêm BOM nên đọc JSON báo lỗi. Đọc bằng mã hóa `utf-8-sig`. **[Đã sửa]**
- `Invoke-RestMethod` hiển thị sai tiếng (ký tự lạ như "â") dù API trả UTF-8 đúng. Chỉ là lỗi hiển thị của PowerShell, không ảnh hưởng dữ liệu.
- Trong terminal mới của phiên làm việc, lệnh `uv` không nằm trong PATH. Gọi thẳng `.venv\Scripts\...`. **[Đã sửa]**
- Test tích hợp có thể xóa dữ liệu thật trong database dev. Test nay dùng database riêng `tg_discovery_test`. **[Đã sửa]**
- Server dev do mình bật chạy nền trong phiên làm việc nên sẽ tắt khi phiên kết thúc. Bạn tự bật lại bằng lệnh trong README. **[Còn mở]**

## 2. Chất lượng code (ruff, pyright strict, test)

- `ruff format --check` báo lỗi file markdown của guide. Thêm `extend-exclude = ["*.md"]`. **[Đã sửa]**
- pyright strict từ chối `Settings(_env_file=None)`. Thêm `load_settings()` và để test dùng biến môi trường với fixture cô lập. **[Đã sửa]**
- Kiểu trả về cũ của `@asynccontextmanager` bị cảnh báo lỗi thời. Đổi sang `AsyncGenerator[...]`. **[Đã sửa]**
- ruff N818 về tên exception. Đổi `BudgetExceeded` thành `BudgetExceededError` và `_SendFailure` thành `_SendError`. **[Đã sửa]**
- Hai giá trị mẫu mình tự đưa vào test là sai: geohash của Bangkok và punycode. Đã kiểm lại bằng hai nguồn độc lập, geohash Bangkok chỉ còn là test chống hồi quy. **[Đã sửa]**
- pyright strict báo lỗi trong test (TypedDict, `Sequence[str]`, danh sách gọi của respx không có kiểu). Đã thêm kiểu. **[Đã sửa]**
- Test "pilot rehearsal" vỡ khi thêm SerpApi (đòi key và route giả). Đã cập nhật test. **[Đã sửa]**
- `ruff format` đổi `–` thành dấu gạch ngang thật và ruff RUF001 phàn nàn. Thêm `# noqa`. **[Đã sửa]**
- Chuyển registry nhà cung cấp từ `evals/` sang `providers/` làm hỏng một test vì câu thông báo đổi. Thêm tham số `hint` cho từng nơi dùng. **[Đã sửa]**
- Docstring của migration thiếu dấu chấm (D415) và vài chỗ type-check nhỏ khác. **[Đã sửa]**

## 3. Bộ thử nghiệm (bake-off harness)

- Bộ so khớp gộp nhầm "Sauna 24" với "Sauna 25". Thêm luật so sánh chữ số. **[Đã sửa]**
- Dry-run ra một cụm 100 dòng vì mã mô phỏng đánh theo vị trí. Mã nay sinh từ tên. **[Đã sửa]**
- Bài kiểm tra độ tươi (canary) làm phình chi phí và số đo. Giảm còn 4 câu hỏi và loại khỏi số đo chính. **[Đã sửa]**
- Bảng gán nhãn "mù" lộ tên nguồn qua `source_url`. Dùng host trung tính và lọc các tên miền của nhà cung cấp (nay có cả google.com). **[Đã sửa]**
- `MeteredHttp` bỏ qua `estimated_cost_usd` khi nhà cung cấp không báo giá (do một subagent phát hiện). Đã sửa và thêm test. **[Đã sửa]**
- Báo cáo không hiện điều khoản của nhà cung cấp. Đã thêm cột. **[Đã sửa]**
- Câu SQL event trong guide thiếu cột `city` mà bộ nạp cần, trong khi tài liệu từng nói "dùng nguyên văn". Đã sửa tài liệu và đưa vào bản SQL chỉnh (chưa chạy thử trên database thật). **[Đã sửa]**
- CLAUDE.md từng ghi "câu hỏi 9 của guide" nhưng guide chỉ có 8 câu. **[Đã sửa]**
- Chưa có golden set (danh sách venue/event TravelGay đang có) và nhãn của admin, nên recall và precision chưa đo được. **[Cần bạn]**

## 4. Điều khoản và pháp lý

- Brave cấm lưu kết quả và cấm dùng để đánh giá dịch vụ AI. Parallel cấm cache, lập database từ kết quả và chia sẻ benchmark. Hai nhà cung cấp này bị khóa cho đến khi chủ dự án nêu tên chấp nhận. **[Cần bạn]**
- Điều khoản Exa và Tavily chỉ do trợ lý đọc, chưa kiểm lại. Perplexity trả HTTP 403 nên chưa đọc được. File PDF điều khoản Exa không mở được (thiếu poppler). **[Còn mở]**
- Điều khoản SerpApi đọc qua công cụ tóm tắt, không phải do luật sư đọc. **[Còn mở]**
- Dữ liệu Google qua SerpApi là vùng xám (Google đang kiện SerpApi). Chỉ giữ tên, địa chỉ, tọa độ, link và mã địa điểm; bỏ đánh giá, ảnh, giờ mở cửa, điện thoại, mô tả. Chủ dự án đồng ý cho bản thử ngày 2026-10-08. **[Cần bạn: hỏi pháp chế trước khi dùng cho sản phẩm thật]**
- Máy chủ Overpass công cộng không dành cho dùng thương mại (nên tự host), và dữ liệu OSM theo giấy phép ODbL phải ghi "© OpenStreetMap contributors" khi đăng. **[Còn mở]**

## 5. Guide lỗi thời

- Guide (viết 2026-10-07) coi Google Events là nguồn event chính. Thực tế SerpApi đã tắt Google Events API (changelog 24 đến 30 tháng 8) và DataForSEO tắt ngày 15 tháng 9. Trang tài liệu của SerpApi vẫn ghi `google_events`, nhưng gọi thật trả HTTP 400 "Unsupported `google_events` search engine". Đã viết lại adapter để đọc ô sự kiện trong Google Search (không có link, giá, mô tả). **[Đã sửa]**
- Hệ quả: không còn nguồn event có cấu trúc kèm link. Event dựa vào Exa và ô events của Google. Nên cân nhắc danh sách nguồn theo dõi, vì 4 trang cho khoảng 2/3 event của Exa. **[Còn mở]**
- Bài học: giá và API đổi nhanh, trang tài liệu có thể cũ, nên phải thử gọi thật.

## 6. Lần gọi API thật đầu tiên

- Bedrock: URL `.../openai/v1` báo "model does not support /openai/v1/responses". Model gpt-oss cần `/v1` trên host `bedrock-mantle`. File `.env` của bạn còn dùng URL cũ. **[Cần bạn sửa `.env`]**
- Bedrock gpt-oss (định dạng Harmony) từ chối `tool_choice` chỉ định hàm ("Only 'auto' tool_choice is supported") và trả HTTP 200 kèm `status: failed`. Chuyển sang `auto` kèm câu yêu cầu gọi hàm, và lỗi `failed` nay được báo rõ lý do. **[Đã sửa]**
- Model trả `venue_type` và `address` dưới dạng danh sách nên mọi venue từ Exa bị loại (0/25 hợp lệ trong mẫu thử, 51/283 mục bị loại cả lần chạy). Đã đọc danh sách thành chuỗi, venue của Exa tăng từ 47 lên 105 và không còn mục nào bị loại. **[Đã sửa]**
- OSM Overpass trả HTTP 406 vì User-Agent chứa email giả `admin@example.com`. Cần email thật trong `NOMINATIM_USER_AGENT`. **[Cần bạn sửa `.env`]**
- Overpass công cộng giới hạn tốc độ: Phuket bị lỗi 429 sau 2 lần thử (khoảng 105 giây), Bangkok phải thử lại. **[Còn mở]**
- SerpApi Google Search chậm: 3/12 câu event quá 30 giây ở lần thử đầu. Đã tăng thời gian chờ lên 90 giây nhưng ở lần chạy qua API vẫn có 5/12 câu quá 90 giây. **[Còn mở]**
- SerpApi gói miễn phí: 250 lượt mỗi tháng, tối đa 50 lượt mỗi giờ. Chạy từng thành phố một. **[Đã ghi chú]**
- Một lần AI trả lời bằng chữ thay vì gọi hàm ("không có lời gọi emit_candidates") nên mất kết quả của câu hỏi đó. Nên thử lại tự động. **[Còn mở]**
- Nhiều câu trích dẫn của AI bị loại vì không khớp nguyên văn với trang (70 đến 84 câu mỗi lần chạy). Cần chỉnh cách kiểm tra. **[Còn mở]**
- Exa trả cả sự kiện đã diễn ra (trang đăng trong 60 ngày nhưng sự kiện đã qua). 58 ứng viên bị loại vì hết hạn. **[Đã xử lý bằng bước chấm điểm]**
- Ngày event trên Google không có năm, nên AI suy ra lần diễn ra kế tiếp và đánh dấu `date_inferred`.
- Trường `type` của ô events đôi khi là tên venue (ví dụ "Slate"). Chưa kiểm tra tác động. **[Còn mở]**

## 7. Chất lượng dữ liệu tìm được

- OpenStreetMap có rất ít venue (Bangkok 10, Chiang Mai 8, New York 36) và nhiều chỗ trống, nhưng dữ liệu của những venue có thì đầy đủ nhất.
- Google Maps cho nhiều venue nhất nhưng 96/221 bản nháp không có loại "gay", nên thành ứng viên chưa kiểm chứng LGBTQ+.
- Chỉ 3/167 venue có đủ cả 3 nguồn. Thử luật gộp lỏng hơn chỉ thêm khoảng 9 cặp, một số cặp sai (ví dụ "Bureau of General Services—Queer Division" với "The Bureau Cafe"). Cần luật gộp thận trọng hơn. **[Còn mở]**
- Event từ Exa và từ Google không gộp được với nhau (0/76), vì cùng một sự kiện nhưng tên viết khác. **[Còn mở]**
- Event từ Google không có link, giá, mô tả nên không kiểm chứng được. **[Còn mở]**
- Chưa có bước verify (mở trang chính thức để kiểm tra) và chưa so với dữ liệu TravelGay đang có, nên danh sách chưa lọc bớt những thứ đã có. **[Còn mở]**
- Màn hình của bạn cần ảnh, tên và điện thoại ban tổ chức, số người dự kiến, topics. Những thứ này chưa được thu thập, hầu hết trường còn trống. **[Còn mở]**

## 8. Bảo mật

- Key SerpApi hiện rõ trong ảnh chụp màn hình gửi trong chat (key Exa chỉ lộ một phần). **[Cần bạn đổi key]**
- Mình không mở nội dung `.env`, chỉ kiểm tra key bằng lệnh `plan`. Key không được ghi vào file kết quả (có test kiểm tra).
- Nội dung trang web được coi là không đáng tin: AI không có quyền ghi dữ liệu, kết quả phải qua kiểm tra khuôn mẫu, mọi URL đi qua lớp chặn SSRF. **[Đã làm từ đầu]**

## 9. Hiểu nhầm phạm vi và giao tiếp

- Bạn mong thấy Swagger để thử API, nhưng lúc đó mới có công cụ dòng lệnh và API chỉ có `/health`. Đã dựng API đợt đầu cùng Swagger. **[Đã sửa]**
- Giải thích ban đầu dùng nhiều thuật ngữ (M0, M1, bake-off, AI search) nên bạn không theo kịp. Đã chuyển sang giải thích đơn giản bằng tiếng Việt. **[Đã sửa]**
- Mình chạy thử OpenStreetMap thật (miễn phí) mà không hỏi trước, làm bạn thắc mắc "bạn vừa làm gì". Nay nói rõ trước khi gọi ra ngoài.
- Mockup giao diện mâu thuẫn với guide: nút "Approve & save to warehouse" trong khi hệ thống chỉ ghi outbox; "Owner name · phone" trong khi quy tắc cấm lưu thông tin cá nhân của người riêng tư; nguồn Facebook và Instagram ngoài phạm vi bản đầu; tab "Scrape data from URLs", lịch chạy tự động và thông báo chưa có. **[Cần bạn]**
- Hầu hết 8 câu hỏi của guide chưa được trả lời (danh sách TravelGay, người gán nhãn, xác thực, đường đăng bài, nước có rủi ro pháp lý cao...), nên pilot đầy đủ chưa chạy được. **[Cần bạn]**

## 10. Việc còn mở

- [ ] Sửa 3 dòng trong `.env`: URL Bedrock có `/v1`, email thật trong `NOMINATIM_USER_AGENT`, và `ACCEPTED_TERMS=serpapi_events,serpapi_maps`.
- [ ] Đổi key SerpApi.
- [ ] Hỏi pháp chế về dữ liệu Google trước khi dùng cho sản phẩm thật.
- [ ] Đọc điều khoản Exa mục 4.2(a); kiểm lại điều khoản Tavily và Perplexity.
- [ ] Cung cấp danh sách venue/event TravelGay đang có (CSV) và người gán nhãn.
- [ ] Quyết định "Approve" nghĩa là gì (chỉ outbox hay đăng thật) và quy tắc về thông tin ban tổ chức.
- [ ] Làm bước verify, so với dữ liệu TravelGay đã có, tiến độ trực tiếp (SSE), sửa trước khi duyệt.
- [ ] Gộp trùng event giữa các nguồn và luật gộp venue thận trọng hơn.
- [ ] Tự thử lại khi AI không gọi hàm; chỉnh cách kiểm tra trích dẫn.
- [ ] Giải pháp cho SerpApi chậm (ít câu hỏi hơn, hoặc thời gian chờ và số lần thử khác).

## Bài học chung

- Mọi adapter được viết theo tài liệu và test bằng dữ liệu mẫu đều lộ lỗi ở lần gọi thật đầu tiên. Luôn chạy thử nhỏ trước khi chạy đủ.
- Tài liệu của nhà cung cấp và guide có thể cũ. Kiểm bằng một lần gọi thật trước khi dựa vào.
- Dữ liệu từ AI và từ web luôn cần kiểm tra khuôn mẫu và đọc dễ dãi ở chỗ hợp lý (danh sách thay vì chuỗi).
- Mỗi nguồn nhìn một góc khác nhau, nên dùng nhiều nguồn và gộp lại, nhưng phải làm gộp trùng cẩn thận.
