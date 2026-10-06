package tools;

import java.io.*;
import java.nio.charset.*;
import java.nio.file.*;
import java.nio.file.attribute.BasicFileAttributes;
import java.time.*;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.*;
import java.util.regex.*;

/**
 * ==============================================================================
 * 순수 Java 기반 메일 파일(.eml, .mht 등) 독립형 고속 파서 (MailParser.java)
 * ==============================================================================
 * [특징]
 * - 외부 라이브러리(Maven/Gradle/Jar) 없이 JDK 표준 라이브러리(Java SE 11+)만으로 단독 실행 가능
 * - Java 11+: 'java MailParser.java --inbox ...' 로 컴파일 없이 즉시 실행 지원
 * - RFC 2047 인코딩 헤더(=?UTF-8?B?...?=, EUC-KR) 완벽 디코딩
 * - 한글 날짜, 영문 RFC 날짜, ISO 날짜, 파일명 날짜를 자동 복원하는 다중 날짜 파서 탑재
 * - 파싱 결과를 표준 JSON 형식으로 출력하여 Python 에이전트와 완벽 연동
 * ==============================================================================
 */
public class MailParser {

    // RFC 2047 인코딩 헤더 패턴 (=?charset?encoding?encoded_text?=)
    private static final Pattern RFC2047_PATTERN = Pattern.compile(
            "=\\?([^?]+)\\?([BbQq])\\?([^?]+)\\?=", Pattern.CASE_INSENSITIVE);

    public static void main(String[] args) {
        // Windows 콘솔 환경에서도 UTF-8 한글이 깨지지 않도록 강제 설정
        try {
            System.setOut(new PrintStream(new FileOutputStream(FileDescriptor.out), true, StandardCharsets.UTF_8));
            System.setErr(new PrintStream(new FileOutputStream(FileDescriptor.err), true, StandardCharsets.UTF_8));
        } catch (Exception ignored) {}

        String inboxDir = "";
        String sentDir = "";
        String targetDate = "";
        String myEmail = "developer@company.com";
        String extensionsStr = ".eml,.mht,.mhtml,.txt,.mail";
        String outputFile = "";

        // CLI 인자 파싱
        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--inbox":
                    if (i + 1 < args.length) inboxDir = args[++i];
                    break;
                case "--sent":
                    if (i + 1 < args.length) sentDir = args[++i];
                    break;
                case "--target-date":
                    if (i + 1 < args.length) targetDate = args[++i];
                    break;
                case "--my-email":
                    if (i + 1 < args.length) myEmail = args[++i];
                    break;
                case "--extensions":
                    if (i + 1 < args.length) extensionsStr = args[++i];
                    break;
                case "--output":
                    if (i + 1 < args.length) outputFile = args[++i];
                    break;
            }
        }

        Set<String> activeExtensions = parseExtensions(extensionsStr);
        boolean isAllDates = targetDate == null || targetDate.isBlank() ||
                targetDate.equalsIgnoreCase("all") || targetDate.equals("*");

        List<Map<String, Object>> parsedItems = new ArrayList<>();

        // 수신 및 발신 폴더 스캔
        if (!inboxDir.isBlank()) {
            scanAndParseFolder(Paths.get(inboxDir), "INBOX", activeExtensions, targetDate, isAllDates, myEmail, parsedItems);
        }
        if (!sentDir.isBlank()) {
            scanAndParseFolder(Paths.get(sentDir), "SENT", activeExtensions, targetDate, isAllDates, myEmail, parsedItems);
        }

        // JSON 직렬화
        String jsonOutput = buildJsonArray(parsedItems);

        if (!outputFile.isBlank()) {
            try {
                Files.writeString(Paths.get(outputFile), jsonOutput, StandardCharsets.UTF_8);
                System.err.println("[Java 파서] 결과 저장 완료: " + outputFile + " (" + parsedItems.size() + "건)");
            } catch (IOException e) {
                System.err.println("[Java 파서] 파일 저장 실패: " + e.getMessage());
                System.out.println(jsonOutput);
            }
        } else {
            // 표준 출력(stdout)으로 JSON 전달
            System.out.println(jsonOutput);
        }
    }

    private static Set<String> parseExtensions(String raw) {
        Set<String> set = new HashSet<>();
        if (raw == null || raw.isBlank()) return set;
        for (String s : raw.split(",")) {
            String clean = s.trim().toLowerCase();
            if (!clean.isEmpty()) {
                if (!clean.startsWith(".")) clean = "." + clean;
                set.add(clean);
            }
        }
        return set;
    }

    private static void scanAndParseFolder(Path dir, String folderType, Set<String> extensions,
                                          String targetDate, boolean isAllDates, String myEmail,
                                          List<Map<String, Object>> resultList) {
        if (!Files.exists(dir) || !Files.isDirectory(dir)) {
            System.err.println("[Java 파서] 폴더가 존재하지 않음: " + dir);
            return;
        }

        try (var stream = Files.walk(dir)) {
            stream.filter(Files::isRegularFile)
                  .filter(p -> matchesExtension(p, extensions))
                  .sorted()
                  .forEach(file -> {
                      try {
                          Map<String, Object> item = parseMailFile(file, folderType, myEmail);
                          if (item != null) {
                              String dateStr = (String) item.get("date_str");
                              if (isAllDates || targetDate.equals(dateStr)) {
                                  resultList.add(item);
                              }
                          }
                      } catch (Exception e) {
                          System.err.println("[Java 파서 경고] " + file.getFileName() + " 파싱 오류: " + e.getMessage());
                      }
                  });
        } catch (IOException e) {
            System.err.println("[Java 파서 오류] 폴더 순회 실패 (" + dir + "): " + e.getMessage());
        }
    }

    private static boolean matchesExtension(Path file, Set<String> extensions) {
        if (extensions.contains("*") || extensions.contains("all")) return true;
        String fileName = file.getFileName().toString().toLowerCase();
        for (String ext : extensions) {
            if (fileName.endsWith(ext)) return true;
        }
        return false;
    }

    public static Map<String, Object> parseMailFile(Path file, String folderType, String myEmail) {
        byte[] rawBytes;
        long mtime;
        try {
            rawBytes = Files.readAllBytes(file);
            mtime = Files.getLastModifiedTime(file).toMillis();
        } catch (IOException e) {
            return null;
        }

        String rawContent = tryDecodeString(rawBytes);

        // 헤더와 본문 영역 분리
        String headerSection = "";
        String bodySection = "";

        int splitIdx = rawContent.indexOf("\r\n\r\n");
        if (splitIdx != -1) {
            headerSection = rawContent.substring(0, splitIdx);
            bodySection = rawContent.substring(splitIdx + 4);
        } else {
            splitIdx = rawContent.indexOf("\n\n");
            if (splitIdx != -1) {
                headerSection = rawContent.substring(0, splitIdx);
                bodySection = rawContent.substring(splitIdx + 2);
            } else {
                headerSection = rawContent;
                bodySection = rawContent;
            }
        }

        // 헤더 맵 추출 (멀티라인 언폴딩 포함)
        Map<String, String> headers = extractHeaders(headerSection);

        String subject = decodeMimeHeader(headers.getOrDefault("subject", file.getFileName().toString()));
        String sender = decodeMimeHeader(headers.getOrDefault("from", "발신자 미상"));
        String receiver = decodeMimeHeader(headers.getOrDefault("to", "수신자 미상"));

        // 날짜 파싱 (만능 복원)
        String rawDateHeader = headers.get("date");
        LocalDateTime mailDateTime = extractDateTime(rawDateHeader, headers, rawContent, file.getFileName().toString(), mtime);

        String dateStr = mailDateTime.format(DateTimeFormatter.ofPattern("yyyy-MM-dd"));
        String mailDate = mailDateTime.format(DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss"));

        // 본문 텍스트 정제
        String cleanBody = extractCleanBody(bodySection, headers);
        if (cleanBody.length() > 1500) {
            cleanBody = cleanBody.substring(0, 1500);
        }

        boolean isMySent = sender.toLowerCase().contains(myEmail.toLowerCase()) || "SENT".equalsIgnoreCase(folderType);

        Map<String, Object> item = new LinkedHashMap<>();
        item.put("id", file.getFileName().toString());
        item.put("file_path", file.toAbsolutePath().toString());
        item.put("folder_type", folderType);
        item.put("subject", subject.isBlank() ? file.getFileName().toString() : subject);
        item.put("sender", sender);
        item.put("receiver", receiver);
        item.put("mail_date", mailDate);
        item.put("date_str", dateStr);
        item.put("body_clean", cleanBody);
        item.put("is_my_sent", isMySent);

        return item;
    }

    private static String tryDecodeString(byte[] bytes) {
        Charset[] candidates = {
            StandardCharsets.UTF_8,
            Charset.forName("MS949"),
            Charset.forName("EUC-KR"),
            StandardCharsets.ISO_8859_1
        };
        for (Charset cs : candidates) {
            try {
                CharsetDecoder decoder = cs.newDecoder()
                        .onMalformedInput(CodingErrorAction.REPORT)
                        .onUnmappableCharacter(CodingErrorAction.REPORT);
                return decoder.decode(java.nio.ByteBuffer.wrap(bytes)).toString();
            } catch (Exception ignored) {
            }
        }
        return new String(bytes, StandardCharsets.UTF_8);
    }

    private static Map<String, String> extractHeaders(String headerText) {
        Map<String, String> map = new HashMap<>();
        String[] lines = headerText.split("\r?\n");
        String currentKey = null;
        StringBuilder currentVal = new StringBuilder();

        for (String line : lines) {
            if (line.startsWith(" ") || line.startsWith("\t")) {
                if (currentKey != null) {
                    currentVal.append(" ").append(line.trim());
                }
            } else {
                if (currentKey != null) {
                    map.put(currentKey.toLowerCase(), currentVal.toString().trim());
                }
                int colonIdx = line.indexOf(':');
                if (colonIdx != -1) {
                    currentKey = line.substring(0, colonIdx).trim();
                    currentVal = new StringBuilder(line.substring(colonIdx + 1).trim());
                } else {
                    currentKey = null;
                }
            }
        }
        if (currentKey != null) {
            map.put(currentKey.toLowerCase(), currentVal.toString().trim());
        }
        return map;
    }

    public static String decodeMimeHeader(String raw) {
        if (raw == null || raw.isBlank()) return "";
        Matcher matcher = RFC2047_PATTERN.matcher(raw);
        StringBuffer sb = new StringBuffer();

        while (matcher.find()) {
            String charsetName = matcher.group(1);
            String encoding = matcher.group(2).toUpperCase();
            String encodedText = matcher.group(3);

            try {
                byte[] decodedBytes;
                if ("B".equals(encoding)) {
                    decodedBytes = Base64.getDecoder().decode(encodedText.replaceAll("\\s+", ""));
                } else {
                    decodedBytes = decodeQuotedPrintable(encodedText);
                }
                Charset cs = Charset.forName(charsetName);
                String decoded = new String(decodedBytes, cs);
                matcher.appendReplacement(sb, Matcher.quoteReplacement(decoded));
            } catch (Exception e) {
                matcher.appendReplacement(sb, Matcher.quoteReplacement(matcher.group(0)));
            }
        }
        matcher.appendTail(sb);
        return sb.toString().trim();
    }

    private static byte[] decodeQuotedPrintable(String s) {
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '_') {
                buffer.write(' ');
            } else if (c == '=' && i + 2 < s.length()) {
                try {
                    int b = Integer.parseInt(s.substring(i + 1, i + 3), 16);
                    buffer.write(b);
                    i += 2;
                } catch (NumberFormatException e) {
                    buffer.write(c);
                }
            } else {
                buffer.write((byte) c);
            }
        }
        return buffer.toByteArray();
    }

    private static LocalDateTime extractDateTime(String dateHeader, Map<String, String> headers, String content, String fileName, long mtime) {
        if (dateHeader != null && !dateHeader.isBlank()) {
            // 1. RFC 1123 / 2822 표준
            try {
                return ZonedDateTime.parse(dateHeader, DateTimeFormatter.RFC_1123_DATE_TIME).toLocalDateTime();
            } catch (Exception ignored) {}

            // 2. 정규식 추출
            LocalDateTime dt = parseRegexDate(dateHeader);
            if (dt != null) return dt;
        }

        // 3. Received 헤더 세미콜론 뒤 타임스탬프
        String received = headers.get("received");
        if (received != null && received.contains(";")) {
            String ts = received.substring(received.lastIndexOf(';') + 1).trim();
            try {
                return ZonedDateTime.parse(ts, DateTimeFormatter.RFC_1123_DATE_TIME).toLocalDateTime();
            } catch (Exception ignored) {}
            LocalDateTime dt = parseRegexDate(ts);
            if (dt != null) return dt;
        }

        // 4. 본문 상단 헤더 텍스트에서 한글/ISO 날짜 정규식 추출
        LocalDateTime contentDt = parseRegexDate(content.substring(0, Math.min(content.length(), 2000)));
        if (contentDt != null) return contentDt;

        // 5. 파일명에서 날짜 추출 (20261006 또는 2026-10-06)
        LocalDateTime fileDt = parseRegexDate(fileName);
        if (fileDt != null) return fileDt;

        // 6. 파일 수정일(mtime) 폴백
        return LocalDateTime.ofInstant(Instant.ofEpochMilli(mtime), ZoneId.systemDefault());
    }

    private static LocalDateTime parseRegexDate(String text) {
        if (text == null) return null;

        // 1. YYYY-MM-DD 또는 YYYY.MM.DD 또는 YYYY/MM/DD
        Matcher m1 = Pattern.compile("(\\d{4})[-./](\\d{1,2})[-./](\\d{1,2})").matcher(text);
        if (m1.find()) {
            try {
                return LocalDate.of(Integer.parseInt(m1.group(1)),
                                    Integer.parseInt(m1.group(2)),
                                    Integer.parseInt(m1.group(3))).atStartOfDay();
            } catch (Exception ignored) {}
        }

        // 2. YYYY년 MM월 DD일
        Matcher m2 = Pattern.compile("(\\d{4})\\s*년\\s*(\\d{1,2})\\s*월\\s*(\\d{1,2})\\s*일").matcher(text);
        if (m2.find()) {
            try {
                return LocalDate.of(Integer.parseInt(m2.group(1)),
                                    Integer.parseInt(m2.group(2)),
                                    Integer.parseInt(m2.group(3))).atStartOfDay();
            } catch (Exception ignored) {}
        }

        // 3. 파일명 8자리 숫자 (20261006)
        Matcher m3 = Pattern.compile("(20\\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\\d|3[01])").matcher(text);
        if (m3.find()) {
            try {
                return LocalDate.of(Integer.parseInt(m3.group(1)),
                                    Integer.parseInt(m3.group(2)),
                                    Integer.parseInt(m3.group(3))).atStartOfDay();
            } catch (Exception ignored) {}
        }

        return null;
    }

    private static String extractCleanBody(String bodyText, Map<String, String> headers) {
        if (bodyText == null || bodyText.isBlank()) return "";

        String text = bodyText;
        // HTML 태그 제거
        text = text.replaceAll("(?is)<(script|style)[^>]*>.*?</\\1>", " ");
        text = text.replaceAll("<[^>]+>", " ");

        // HTML 엔티티 치환
        text = text.replace("&nbsp;", " ")
                   .replace("&lt;", "<")
                   .replace("&gt;", ">")
                   .replace("&amp;", "&")
                   .replace("&quot;", "\"")
                   .replace("&#39;", "'");

        // 공백 및 줄바꿈 정리
        text = text.replaceAll("[ \t]+", " ");
        text = text.replaceAll("\r\n|\r", "\n");
        text = text.replaceAll("\n\\s*\n+", "\n\n");
        return text.trim();
    }

    // 자체 내장 안전 JSON 직렬화 (외부 의존성 제로)
    private static String buildJsonArray(List<Map<String, Object>> list) {
        StringBuilder sb = new StringBuilder();
        sb.append("[\n");
        for (int i = 0; i < list.size(); i++) {
            Map<String, Object> map = list.get(i);
            sb.append("  {\n");
            int j = 0;
            for (Map.Entry<String, Object> e : map.entrySet()) {
                sb.append("    \"").append(escapeJson(e.getKey())).append("\": ");
                Object val = e.getValue();
                if (val instanceof Boolean) {
                    sb.append(val);
                } else if (val instanceof Number) {
                    sb.append(val);
                } else {
                    sb.append("\"").append(escapeJson(String.valueOf(val))).append("\"");
                }
                if (++j < map.size()) sb.append(",");
                sb.append("\n");
            }
            sb.append("  }");
            if (i + 1 < list.size()) sb.append(",");
            sb.append("\n");
        }
        sb.append("]\n");
        return sb.toString();
    }

    private static String escapeJson(String s) {
        if (s == null) return "";
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\b': sb.append("\\b"); break;
                case '\f': sb.append("\\f"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c <= 0x1F) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        return sb.toString();
    }
}
