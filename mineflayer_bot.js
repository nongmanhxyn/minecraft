const mineflayer = require('mineflayer');
const pathfinder = require('mineflayer-pathfinder').pathfinder;
const { GoalNear } = require('mineflayer-pathfinder').goals;
const Vec3 = require('vec3');

const args = process.argv.slice(2);
const username = args[0] || 'Bot';
const host = args[1] || 'localhost';
const port = parseInt(args[2]) || 25565;
const version = args[3] || '1.21.1';

let bot;
let reconnectAttempts = 0;
const maxReconnectDelay = 30000; // tối đa 30 giây

function createBot() {
    const botInstance = mineflayer.createBot({ host, port, username, version });
    botInstance.loadPlugin(pathfinder);

    // Gửi trạng thái định kỳ mỗi giây
    const statusInterval = setInterval(() => {
        if (!botInstance.entity) return;
        const status = {
            event: 'status',
            position: [
                Math.round(botInstance.entity.position.x * 10) / 10,
                Math.round(botInstance.entity.position.y * 10) / 10,
                Math.round(botInstance.entity.position.z * 10) / 10
            ],
            health: botInstance.health,
            time: (botInstance.time.timeOfDay < 13000 || botInstance.time.timeOfDay > 23000) ? 'day' : 'night',
            entities: Object.values(botInstance.entities)
                .filter(e => e !== botInstance.entity)
                .map(e => ({
                    name: e.name || e.username || e.type,
                    type: e.type,
                    position: [
                        Math.round(e.position.x * 10) / 10,
                        Math.round(e.position.y * 10) / 10,
                        Math.round(e.position.z * 10) / 10
                    ],
                    distance: botInstance.entity.position.distanceTo(e.position).toFixed(2)
                }))
        };
        process.stdout.write(JSON.stringify(status) + '\n');
    }, 1000);

    // Đọc lệnh từ stdin
    const readline = require('readline');
    const rl = readline.createInterface({
        input: process.stdin,
        output: process.stdout,
        terminal: false
    });

    rl.on('line', (line) => {
        try {
            const cmd = JSON.parse(line);
            handleCommand(cmd, botInstance);
        } catch (e) {
            console.error('Invalid command:', e);
        }
    });

    function handleCommand(cmd, bot) {
        switch (cmd.action) {
            case 'moveTo': {
                const { x, y, z } = cmd;
                bot.pathfinder.setGoal(new GoalNear(x, y, z, 1));
                break;
            }
            case 'attack': {
                const hostile = bot.nearestEntity(entity =>
                    entity.type === 'mob' &&
                    entity.mobType !== 'Player' &&
                    entity.mobType !== 'Armor Stand'
                );
                if (hostile) bot.attack(hostile);
                else console.error('No hostile nearby');
                break;
            }
            case 'mineBlock': {
                const block = bot.blockAt(new Vec3(cmd.x, cmd.y, cmd.z));
                if (block && bot.canDigBlock(block)) {
                    bot.dig(block, (err) => {
                        if (err) console.error('Dig error:', err);
                    });
                } else {
                    console.error('Cannot mine block');
                }
                break;
            }
            case 'placeBlock': {
                const placeAgainst = bot.blockAt(new Vec3(cmd.x, cmd.y - 1, cmd.z));
                if (placeAgainst) {
                    bot.placeBlock(placeAgainst, new Vec3(0, 1, 0), (err) => {
                        if (err) console.error('Place error:', err);
                    });
                } else {
                    console.error('No block to place against');
                }
                break;
            }
            default:
                console.error('Unknown action:', cmd.action);
        }
    }

    // ===== FALLBACK: Tự động respawn khi chết =====
    botInstance.on('death', () => {
        console.log(`[${username}] Đã chết, tự động respawn...`);
        setTimeout(() => {
            botInstance.respawn();
        }, 1000);
    });

    // ===== FALLBACK: Reconnect khi bị kick hoặc mất kết nối =====
    botInstance.on('end', (reason) => {
        console.log(`[${username}] Mất kết nối: ${reason}`);
        clearInterval(statusInterval);
        attemptReconnect();
    });

    botInstance.on('kicked', (reason) => {
        console.log(`[${username}] Bị kick: ${reason}`);
        clearInterval(statusInterval);
        attemptReconnect();
    });

    botInstance.on('error', (err) => {
        console.error(`[${username}] Lỗi:`, err);
        // Không gọi attemptReconnect ở đây vì 'end' sẽ được gọi sau đó
    });

    return botInstance;
}

function attemptReconnect() {
    const delay = Math.min(1000 * Math.pow(2, reconnectAttempts), maxReconnectDelay);
    reconnectAttempts++;
    console.log(`[${username}] Thử kết nối lại sau ${delay/1000}s...`);
    setTimeout(() => {
        bot = createBot();
    }, delay);
}

// Khởi động bot lần đầu
bot = createBot();

process.on('uncaughtException', (err) => console.error('Uncaught:', err));
