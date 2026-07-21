const mineflayer = require('mineflayer');
const pathfinder = require('mineflayer-pathfinder').pathfinder;
const { GoalNear } = require('mineflayer-pathfinder').goals;
const Vec3 = require('vec3');
const readline = require('readline');

const args = process.argv.slice(2);
const username = args[0] || 'Bot';
const host = args[1] || 'testserverhaha.aternos.me';
const port = parseInt(args[2]) || 25565;
const version = args[3] || '1.21.11';

// Danh sách mob thù địch dùng để chọn mục tiêu tấn công (mineflayer/minecraft-data
// không có sẵn cờ "hostile" đáng tin cậy nên liệt kê thủ công).
const HOSTILE_MOBS = new Set([
    'zombie', 'husk', 'drowned', 'skeleton', 'stray', 'wither_skeleton',
    'spider', 'cave_spider', 'creeper', 'enderman', 'endermite', 'witch',
    'phantom', 'pillager', 'vindicator', 'evoker', 'vex', 'ravager',
    'blaze', 'ghast', 'magma_cube', 'slime', 'guardian', 'elder_guardian',
    'shulker', 'hoglin', 'zoglin', 'piglin_brute', 'silverfish', 'warden',
    'zombie_villager', 'zombified_piglin',
]);

const REACH_DISTANCE = 4.0;       // khoảng cách tối đa để đào/đặt/tấn công không cần di chuyển thêm
const MOVE_TIMEOUT_MS = 20000;    // timeout cho lệnh moveTo
const APPROACH_TIMEOUT_MS = 10000; // timeout cho bước "lại gần" trước khi đào/đặt/tấn công

let bot;
let statusIntervalHandle = null;
let isExiting = false;
let spawnWatchdogHandle = null;

function exitClean(code) {
    if (isExiting) return;
    isExiting = true;
    if (statusIntervalHandle) clearInterval(statusIntervalHandle);
    if (spawnWatchdogHandle) clearTimeout(spawnWatchdogHandle);
    process.exit(code);
}

function sendEvent(payload) {
    try {
        process.stdout.write(JSON.stringify(payload) + '\n');
    } catch (e) {
        console.error(`[${username}] Lỗi ghi stdout:`, e.message);
    }
}

function sendResult(id, ok, message) {
    if (!id) return; // lệnh không có id (không nên xảy ra) thì bỏ qua, không gửi ack
    sendEvent({ event: 'actionResult', id, ok, message });
}

// Race một promise với một timeout thủ công: mineflayer-pathfinder có một lỗi hiếm gặp
// (xem PrismarineJS/mineflayer-pathfinder#205) khiến goto() không bao giờ resolve/reject
// nếu goal nằm ở rìa một block không đầy khối. Timeout ở đây là lưới an toàn cuối cùng.
function withTimeout(promise, ms, onTimeout) {
    let timer;
    const timeout = new Promise((_, reject) => {
        timer = setTimeout(() => {
            if (onTimeout) onTimeout();
            reject(new Error('Timeout'));
        }, ms);
    });
    return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

async function gotoNear(x, y, z, range, timeoutMs) {
    const goal = new GoalNear(x, y, z, range);
    await withTimeout(
        bot.pathfinder.goto(goal),
        timeoutMs,
        () => bot.pathfinder.stop()
    );
}

const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
    terminal: false,
});

rl.on('line', (line) => {
    if (!bot) return;
    let cmd;
    try {
        cmd = JSON.parse(line);
    } catch (e) {
        console.error(`[${username}] Lệnh không hợp lệ (JSON lỗi):`, e.message);
        return;
    }
    handleCommand(cmd).catch((e) => {
        console.error(`[${username}] Lỗi xử lý lệnh '${cmd && cmd.action}':`, e.message);
        sendResult(cmd && cmd.id, false, e.message);
    });
});

async function handleCommand(cmd) {
    const id = cmd.id;

    switch (cmd.action) {
        case 'moveTo': {
            const x = Math.floor(cmd.x);
            const y = Math.floor(cmd.y);
            const z = Math.floor(cmd.z);
            console.error(`[${username}] Đang di chuyển tới: (${x}, ${y}, ${z})`);
            try {
                await gotoNear(x, y, z, 1, MOVE_TIMEOUT_MS);
                sendResult(id, true, `Đã tới gần (${x}, ${y}, ${z})`);
            } catch (err) {
                sendResult(id, false, `Không tới được (${x}, ${y}, ${z}): ${err.message}`);
            }
            break;
        }

        case 'attack': {
            const hostile = bot.nearestEntity((entity) =>
                entity.type === 'mob' && HOSTILE_MOBS.has((entity.name || '').toLowerCase())
            );
            if (!hostile) {
                sendResult(id, false, 'Không thấy mob thù địch nào gần đây');
                break;
            }
            try {
                const dist = bot.entity.position.distanceTo(hostile.position);
                if (dist > REACH_DISTANCE) {
                    await gotoNear(
                        Math.floor(hostile.position.x),
                        Math.floor(hostile.position.y),
                        Math.floor(hostile.position.z),
                        2,
                        APPROACH_TIMEOUT_MS
                    );
                }
                if (!hostile.isValid) {
                    sendResult(id, false, 'Mục tiêu đã biến mất trước khi tấn công được');
                    break;
                }
                console.error(`[${username}] Tấn công ${hostile.name}`);
                bot.attack(hostile);
                sendResult(id, true, `Đã tấn công ${hostile.name}`);
            } catch (err) {
                sendResult(id, false, `Lỗi khi tiếp cận/tấn công: ${err.message}`);
            }
            break;
        }

        case 'mineBlock': {
            const pos = new Vec3(cmd.x, cmd.y, cmd.z);
            const block = bot.blockAt(pos);
            if (!block) {
                sendResult(id, false, `Không xác định được block tại (${cmd.x}, ${cmd.y}, ${cmd.z})`);
                break;
            }
            if (!bot.canDigBlock(block)) {
                sendResult(id, false, `Không thể đào ${block.name} tại (${cmd.x}, ${cmd.y}, ${cmd.z})`);
                break;
            }
            try {
                const dist = bot.entity.position.distanceTo(pos);
                if (dist > REACH_DISTANCE) {
                    await gotoNear(cmd.x, cmd.y, cmd.z, 2, APPROACH_TIMEOUT_MS);
                }
                console.error(`[${username}] Đang đập ${block.name} ở (${cmd.x}, ${cmd.y}, ${cmd.z})`);
                await bot.dig(block);
                sendResult(id, true, `Đã đào ${block.name}`);
            } catch (err) {
                sendResult(id, false, `Lỗi khi đào: ${err.message}`);
            }
            break;
        }

        case 'equipItem': {
            if (!cmd.itemName) {
                sendResult(id, false, 'Thiếu itemName');
                break;
            }
            const item = bot.inventory.items().find((i) => i.name.includes(cmd.itemName));
            if (!item) {
                sendResult(id, false, `Không có ${cmd.itemName} trong túi`);
                break;
            }
            try {
                console.error(`[${username}] Đang trang bị ${item.name} vào ${cmd.destination}`);
                await bot.equip(item, cmd.destination || 'hand');
                sendResult(id, true, `Đã trang bị ${item.name}`);
            } catch (err) {
                sendResult(id, false, `Lỗi khi trang bị: ${err.message}`);
            }
            break;
        }

        case 'placeBlock': {
            if (!cmd.blockName) {
                sendResult(id, false, 'Thiếu blockName');
                break;
            }
            const item = bot.inventory.items().find((i) => i.name.includes(cmd.blockName));
            if (!item) {
                sendResult(id, false, `Không có ${cmd.blockName} để đặt`);
                break;
            }
            try {
                const targetPos = new Vec3(cmd.x, cmd.y, cmd.z);
                const dist = bot.entity.position.distanceTo(targetPos);
                if (dist > REACH_DISTANCE) {
                    await gotoNear(cmd.x, cmd.y, cmd.z, 2, APPROACH_TIMEOUT_MS);
                }
                await bot.equip(item, 'hand');
                const refBlock = bot.blockAt(new Vec3(cmd.x, cmd.y - 1, cmd.z));
                if (!refBlock || refBlock.name === 'air') {
                    sendResult(id, false, `Không có mặt tham chiếu để đặt block tại (${cmd.x}, ${cmd.y}, ${cmd.z})`);
                    break;
                }
                console.error(`[${username}] Đang đặt ${item.name} tại (${cmd.x}, ${cmd.y}, ${cmd.z})`);
                await bot.placeBlock(refBlock, new Vec3(0, 1, 0));
                sendResult(id, true, `Đã đặt ${item.name}`);
            } catch (err) {
                sendResult(id, false, `Lỗi khi đặt block: ${err.message}`);
            }
            break;
        }

        default:
            sendResult(id, false, `Hành động không xác định: ${cmd.action}`);
            break;
    }
}

function createBot() {
    const botInstance = mineflayer.createBot({
        host,
        port,
        username,
        version,
        checkTimeoutInterval: 120000,
    });

    botInstance.loadPlugin(pathfinder);

    // Lưới an toàn: nếu sau 60s vẫn chưa spawn được (vd: kẹt DNS/handshake mà không
    // bắn ra sự kiện error), tự thoát để Python quản lý kết nối lại.
    spawnWatchdogHandle = setTimeout(() => {
        console.error(`[${username}] Không spawn được sau 60s, thoát để thử lại...`);
        exitClean(1);
    }, 60000);

    botInstance.on('spawn', () => {
        console.error(`[${username}] Spawn thành công!`);
        if (spawnWatchdogHandle) {
            clearTimeout(spawnWatchdogHandle);
            spawnWatchdogHandle = null;
        }
        if (botInstance.pathfinder) {
            botInstance.pathfinder.thinkTimeout = 5000;
        }
    });

    statusIntervalHandle = setInterval(() => {
        if (!botInstance.entity) return;
        try {
            const inventory = botInstance.inventory.items().map((i) => ({
                name: i.name,
                count: i.count,
            }));

            const entities = Object.values(botInstance.entities)
                .filter((e) => e !== botInstance.entity && e.position)
                .map((e) => ({
                    name: e.name || e.username || e.type,
                    type: e.type,
                    position: [
                        Math.round(e.position.x),
                        Math.round(e.position.y),
                        Math.round(e.position.z),
                    ],
                    distance: Number(botInstance.entity.position.distanceTo(e.position).toFixed(1)),
                }))
                .sort((a, b) => a.distance - b.distance) // gần nhất trước, để Python lấy top-N cho prompt
                .slice(0, 20);

            const status = {
                event: 'status',
                position: [
                    Math.round(botInstance.entity.position.x),
                    Math.round(botInstance.entity.position.y),
                    Math.round(botInstance.entity.position.z),
                ],
                health: botInstance.health,
                inventory,
                time: (botInstance.time.timeOfDay < 13000 || botInstance.time.timeOfDay > 23000) ? 'day' : 'night',
                entities,
            };
            sendEvent(status);
        } catch (e) {
            console.error(`[${username}] Lỗi khi thu thập status:`, e.message);
        }
    }, 1000);

    botInstance.on('path_update', (r) => {
        if (r.status === 'noPath') console.error(`[${username}] Không tìm thấy đường đi!`);
    });

    botInstance.on('death', () => {
        setTimeout(() => botInstance.respawn(), 1000);
    });

    botInstance.on('end', (reason) => {
        console.error(`[${username}] Mất kết nối (${reason})`);
        exitClean(1);
    });

    botInstance.on('kicked', (reason) => {
        console.error(`[${username}] Bị kick (${reason})`);
        exitClean(1);
    });

    // Trước đây chỉ log mà không thoát: nếu lỗi kết nối (ECONNRESET, ETIMEDOUT, ...)
    // xảy ra mà không kèm theo 'end', tiến trình sẽ treo vô thời hạn và Python
    // không bao giờ biết để reconnect. Nay thoát sạch để Python luôn quản lý được.
    botInstance.on('error', (err) => {
        console.error(`[${username}] Lỗi kết nối:`, err.message);
        exitClean(1);
    });

    return botInstance;
}

bot = createBot();

process.on('uncaughtException', (err) => {
    console.error(`[${username}] Uncaught exception:`, err.message);
    exitClean(1);
});

process.on('unhandledRejection', (reason) => {
    console.error(`[${username}] Unhandled rejection:`, reason && reason.message ? reason.message : reason);
    exitClean(1);
});
